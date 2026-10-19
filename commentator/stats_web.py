"""A small local web page for browsing the season database (`stats-web`).

The stats themselves (summary() and friends) are computed here; stats_site.py builds them into the
static site that this module serves locally and `publish` uploads. The page is stats_web.html. Stats follow the booth's own rules (store.py): mulligans and unattributed
shots are left out, and scoring, GIR, scrambling, sand and putting come from individual-ball rounds
only, since a scramble or alternate-shot score isn't one player's.
"""

import bisect
import json
import re
import sqlite3
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import season_analytics as A

SHARED_BALL = ("scramble", "alternate")
# Putt length buckets in feet (to_pin_yards is stored in yards). They start at GSPro's 4 ft gimme
# circle: anything inside it is conceded, never struck, so the first bucket also takes the rare putt
# struck from closer (gimmes switched off) rather than show a bucket that's always empty.
PUTT_BUCKETS = [(4, 6), (6, 10), (10, 20), (20, 40), (40, None)]
# Approach-by-club only counts shots from this close, so a 3-wood off a par-5 tee isn't an "approach".
APPROACH_MAX_YARDS = 250
_DRIVE = "COALESCE(total_yards, carry_game_yards, carry_raw)"
_TEE_DRIVER = ("club = 'DR' AND is_putt = 0 AND (shot_number = 1 OR lie_before = 'tee' "
               "OR (shot_number IS NULL AND lie_before IS NULL))")


def connect(path: str | Path) -> sqlite3.Connection:
    """Read-only, so viewing can never change the copy (or a live booth's file)."""
    db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _ctx(context: str | None) -> str:
    return {"league": " AND is_league = 1", "practice": " AND is_league = 0"}.get(context or "", "")


def _json(text: str | None) -> dict[int, int]:
    try:
        return {int(k): v for k, v in json.loads(text or "{}").items() if isinstance(v, int)}
    except (TypeError, ValueError, AttributeError):
        return {}


def _pct(hit, tries) -> float | None:
    return round(100.0 * hit / tries, 1) if tries else None


def _card(scores: dict[int, int], pars: dict[int, int]) -> dict[str, Any]:
    """Per-hole score vs par over the holes actually scored."""
    holes = sorted(h for h in scores if h in pars)
    return {
        "holes": [{"hole": h, "par": pars[h], "score": scores[h]} for h in holes],
        "gross": sum(scores[h] for h in holes),
        "par": sum(pars[h] for h in holes),
        "to_par": sum(scores[h] - pars[h] for h in holes),
        "played": len(holes),
    }


def _counts(complete, played) -> bool:
    """As in the booth: a finished round, or a full nine from one quit early."""
    return bool(complete) or (played or 0) >= 9


ROUND_GAP_SECONDS = 30   # a round's sides are stored a moment apart; separate rounds are minutes apart
ROUND_SPAN_SECONDS = 8 * 3600   # no round takes longer: an older shot was in a round never recorded


def round_groups(db: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """The rounds as played: one per end of a round, whatever its number of sides (one row each in
    `rounds`). Returns them oldest first ({round, end, course}; `round` is the lowest row id) and
    each row's round."""
    groups: list[dict[str, Any]] = []
    of_row: dict[int, int] = {}
    for r in db.execute("SELECT id, session_id, ended_at, course FROM rounds ORDER BY ended_at, id"):
        g = groups[-1] if groups else None
        if not (g and g["session"] == r["session_id"] and g["course"] == r["course"]
                and r["ended_at"] - g["end"] <= ROUND_GAP_SECONDS):
            g = {"round": r["id"], "end": r["ended_at"], "course": r["course"], "session": r["session_id"]}
            groups.append(g)
        g["end"] = r["ended_at"]
        of_row[r["id"]] = g["round"]
    return groups, of_row


def shot_rounds(db: sqlite3.Connection) -> dict[int, int]:
    """Each shot's round: shots aren't stored with one, so it's the round that ended next after the
    shot (and after the round before it), when that round was on the shot's course and ended within a
    round's length of it (a booth restart mid-round keeps its shots). Shots of a round that was never
    finished and recorded have none."""
    groups, _ = round_groups(db)
    ends = [g["end"] for g in groups]
    out = {}
    for sid, ts, course in db.execute("SELECT id, ts, course FROM shots"):
        i = bisect.bisect_left(ends, ts)
        # (a course name read off the scorecard is in capitals)
        if i < len(groups) and (groups[i]["course"] or "").casefold() == (course or "").casefold() \
                and groups[i]["end"] - ts <= ROUND_SPAN_SECONDS:
            out[sid] = groups[i]["round"]
    return out


def rounds(db: sqlite3.Connection, context: str | None = None) -> list[dict[str, Any]]:
    out = []
    _, of_row = round_groups(db)
    who: dict[int, set] = {}
    named = dict(db.execute("SELECT id, player FROM shots WHERE player IS NOT NULL AND player != 'unknown'").fetchall())
    for sid, rnd in shot_rounds(db).items():
        if sid in named:
            who.setdefault(rnd, set()).add(named[sid])
    for r in db.execute("SELECT * FROM rounds WHERE 1=1" + _ctx(context) + " ORDER BY ended_at DESC, id DESC"):
        card = _card(_json(r["scores"]), _json(r["hole_pars"]))
        out.append({
            "id": r["id"], "round": of_row.get(r["id"], r["id"]), "ended": r["ended_at"], "date": r["date"],
            "course": r["course"],
            # who played in it (a team side's players too), so the page can show one player's rounds
            "players": sorted(who.get(of_row.get(r["id"], r["id"]), set()) | ({r["side"]} if r["side"] else set())), "format": r["format"] or "individual",
            "side": r["side"], "team": r["team"], "holes": r["holes"],
            "played": r["holes_played"] if r["holes_played"] is not None else r["holes"],
            "complete": bool(r["complete"]), "league": bool(r["is_league"]),
            "gross": card["gross"] if card["played"] else r["gross"],
            "to_par": card["to_par"] if card["played"] else None,
            "fir": [r["fir_hit"], r["fir_tries"]], "gir": [r["gir_hit"], r["gir_tries"]],
            "scramble": [r["scramble_hit"], r["scramble_tries"]], "sand": [r["sand_hit"], r["sand_tries"]],
            "putts": r["putts"], "penalties": r["penalties"], "longest_drive": r["longest_drive"],
            "card": card["holes"],
        })
    return out


def players(db: sqlite3.Connection, context: str | None = None) -> list[dict[str, Any]]:
    """One row per player: season scoring, ball-striking, driving and putting."""
    names = [r[0] for r in db.execute(
        "SELECT player FROM player_rounds WHERE 1=1" + _ctx(context)
        + " UNION SELECT player FROM shots WHERE player IS NOT NULL AND player != 'unknown'"
          " AND attributed = 1 AND mulligan = 0" + _ctx(context) + " ORDER BY 1")]
    return [player(db, name, context) for name in names]


def player(db: sqlite3.Connection, name: str, context: str | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {"name": name}
    fir = [0, 0]
    tot = {k: [0, 0] for k in ("gir", "scramble", "sand", "putts")}
    nines: list[float] = []
    holes = birdies = pars_made = bogeys = doubles = 0
    by_par: dict[int, list[int]] = {3: [0, 0], 4: [0, 0], 5: [0, 0]}
    history = []
    for r in db.execute("SELECT * FROM player_rounds WHERE player = ?" + _ctx(context) + " ORDER BY ended_at",
                        (name,)):
        fir[0] += r["fir_hit"] or 0
        fir[1] += r["fir_tries"] or 0
        if (r["fmt"] or "") in SHARED_BALL:
            continue
        for key, hit, tries in (("gir", "gir_hit", "gir_tries"), ("scramble", "scramble_hit", "scramble_tries"),
                                ("sand", "sand_hit", "sand_tries"), ("putts", "putts", "putt_holes")):
            tot[key][0] += r[hit] or 0
            tot[key][1] += r[tries] or 0
        card = _card(_json(r["scores"]), _json(r["hole_pars"]))
        for h in card["holes"]:
            d = h["score"] - h["par"]
            holes += 1
            birdies += d <= -1
            pars_made += d == 0
            bogeys += d == 1
            doubles += d >= 2
            if h["par"] in by_par:
                by_par[h["par"]][0] += d
                by_par[h["par"]][1] += 1
        if card["played"] and _counts(r["complete"], r["holes_played"] or card["played"]):
            per_nine = card["to_par"] * 9.0 / card["played"]
            nines.append(per_nine)
            history.append({"date": _date(db, r["session_id"]), "course": r["course"], "played": card["played"],
                            "gross": card["gross"], "to_par": card["to_par"]})
    p.update({
        "rounds": len(nines),
        "avg_to_par_9": round(sum(nines) / len(nines), 1) if nines else None,
        "best_to_par_9": round(min(nines), 1) if nines else None,
        "fir_pct": _pct(*fir), "fir": fir,
        "gir_pct": _pct(*tot["gir"]), "gir": tot["gir"],
        "scramble_pct": _pct(*tot["scramble"]), "scramble": tot["scramble"],
        "sand_pct": _pct(*tot["sand"]), "sand": tot["sand"],
        "putts_per_hole": round(tot["putts"][0] / tot["putts"][1], 2) if tot["putts"][1] else None,
        "holes": holes,
        "birdie_pct": _pct(birdies, holes), "par_pct": _pct(pars_made, holes),
        "bogey_pct": _pct(bogeys, holes), "double_pct": _pct(doubles, holes),
        "par_avg": {str(k): round(v[0] / v[1], 2) if v[1] else None for k, v in by_par.items()},
        "history": history,
    })
    shots = " AND player = ? AND attributed = 1 AND mulligan = 0" + _ctx(context)
    d = db.execute(f"SELECT COUNT(*) n, AVG({_DRIVE}) avg, MAX({_DRIVE}) best FROM shots "
                   f"WHERE {_TEE_DRIVER} AND {_DRIVE} IS NOT NULL" + shots, (name,)).fetchone()
    p["drives"] = d["n"]
    p["avg_drive"] = round(d["avg"]) if d["avg"] is not None else None
    p["longest_drive"] = round(d["best"]) if d["best"] is not None else None
    p["shots"] = db.execute("SELECT COUNT(*) FROM shots WHERE 1=1" + shots, (name,)).fetchone()[0]
    p["clubs"] = [dict(r) for r in db.execute(
        "SELECT club, COUNT(*) AS n, ROUND(AVG(finish_yards), 1) AS avg_finish, ROUND(MIN(finish_yards), 1) AS best_finish,"
        " ROUND(AVG(to_pin_yards)) AS avg_from FROM shots WHERE is_putt = 0 AND finish_yards IS NOT NULL"
        " AND club IS NOT NULL AND club NOT IN ('DR', 'PT') AND to_pin_yards <= ?" + shots + " GROUP BY club ORDER BY avg_from DESC",
        (APPROACH_MAX_YARDS, name))]
    p["putting"] = []
    for lo, hi in PUTT_BUCKETS:
        r = db.execute("SELECT COUNT(*) n, SUM(outcome = 'holed') made FROM shots WHERE is_putt = 1"
                       " AND outcome IS NOT NULL AND outcome != 'gimme' AND to_pin_yards >= ? AND to_pin_yards < ?"
                       + shots, (0 if lo == PUTT_BUCKETS[0][0] else lo / 3.0, (hi or 10_000) / 3.0, name)).fetchone()
        p["putting"].append({"range": f"{lo}-{hi} ft" if hi else f"{lo}+ ft", "attempts": r["n"],
                             "made": r["made"] or 0, "pct": _pct(r["made"] or 0, r["n"])})
    return p


def _date(db: sqlite3.Connection, session_id: int) -> str | None:
    r = db.execute("SELECT date FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return r[0] if r else None


ROLES = {"pbp": "play-by-play", "color": "color analyst"}


def predictions(db: sqlite3.Connection, names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Each commentator's make/miss call record, by on-air name (the DB keeps the persona key)."""
    names = names or {}
    return [{"commentator": names.get(r["commentator"], r["commentator"]),
             "role": ROLES.get(r["commentator"], r["commentator"]), "calls": r["n"], "right": r["hits"] or 0, "pct": _pct(r["hits"] or 0, r["n"])}
            for r in db.execute("SELECT commentator, COUNT(*) n, SUM(correct) hits FROM predictions "
                                "WHERE correct IS NOT NULL GROUP BY commentator ORDER BY commentator")]


def records(db: sqlite3.Connection, context: str | None = None, player: str | None = None) -> dict[str, Any]:
    base = (" AND player IS NOT NULL AND player != 'unknown' AND attributed = 1 AND mulligan = 0" + _ctx(context)
            + (" AND player = ?" if player else ""))
    args = (player,) if player else ()
    drive = db.execute(f"SELECT player, {_DRIVE} AS v, course, hole FROM shots WHERE {_TEE_DRIVER} AND {_DRIVE} IS NOT NULL"
                       + base + " ORDER BY v DESC LIMIT 1", args).fetchone()
    approach = db.execute(f"SELECT player, finish_yards AS v, club, outcome, course, hole FROM shots WHERE {A.APPROACH_SQL}"
                          " AND finish_yards IS NOT NULL" + base + " ORDER BY v LIMIT 1", args).fetchone()
    putt = db.execute("SELECT player, to_pin_yards AS v, course, hole FROM shots WHERE is_putt = 1 AND outcome = 'holed'"
                      " AND to_pin_yards IS NOT NULL" + base + " ORDER BY v DESC LIMIT 1", args).fetchone()
    return {"longest_drive": dict(drive) if drive else None, "closest_approach": dict(approach) if approach else None,
            "longest_putt": dict(putt) if putt else None}


def summary(db: sqlite3.Connection, context: str | None = None,
            names: dict[str, str] | None = None) -> dict[str, Any]:
    last = db.execute("SELECT MAX(date) FROM sessions").fetchone()[0]
    holes = A.holes_played(db, context)
    sg, rec, trend = A.sg_summary(holes), A.recovery(holes), A.trends(db, holes, context)
    people = players(db, context)
    for p in people:
        p["sg"] = sg.get(p["name"])
        p["gapping"] = A.gapping(db, p["name"], context)
        p["recovery"] = rec.get(p["name"], [])
        p["trend"] = trend.get(p["name"], [])
    filters = {
        "dates": [r[0] for r in db.execute("SELECT DISTINCT date FROM sessions s WHERE EXISTS"
                                           " (SELECT 1 FROM shots WHERE session_id = s.id) ORDER BY date DESC")],
        "courses": [r[0] for r in db.execute("SELECT DISTINCT course FROM shots WHERE course IS NOT NULL ORDER BY 1")],
        "clubs": [r[0] for r in db.execute("SELECT DISTINCT club FROM shots WHERE club IS NOT NULL ORDER BY 1")],
        "lies": [r[0] for r in db.execute("SELECT DISTINCT lie_before FROM shots WHERE lie_before IS NOT NULL ORDER BY 1")],
    }
    # No league rounds at all (no league file, or the season hasn't started): the page drops the
    # league/practice filter and tags, since every round would be practice.
    has_league = db.execute("SELECT EXISTS (SELECT 1 FROM rounds WHERE is_league = 1)"
                            " OR EXISTS (SELECT 1 FROM shots WHERE is_league = 1)").fetchone()[0] == 1
    return {"context": context or "all", "last_session": last, "has_league": has_league, "players": people,
            "rounds": rounds(db, context), "records": records(db, context), "predictions": predictions(db, names),
            "courses": A.courses(db, context), "highlights": A.highlights(db, holes, context),
            "booth": A.booth(db, names), "broadcasts": A.broadcasts(db), "filters": filters}


def make_server(db_path: str | Path, port: int = 8765, names: dict[str, str] | None = None,
                site: str | Path | None = None) -> ThreadingHTTPServer:
    """Serve the stats page on localhost only (port 0 picks a free one), the same static site that
    `publish` uploads (stats_site.py), built into `site` (a temp folder by default) and rebuilt when
    the database changes. `names` maps persona keys to on-air names."""
    from . import stats_site

    db_path = Path(db_path)
    site = Path(site) if site else Path(tempfile.mkdtemp(prefix="shotcast-stats-"))
    lock = threading.Lock()
    built = {"mtime": None}

    def refresh() -> None:
        with lock:
            mtime = db_path.stat().st_mtime
            if built["mtime"] != mtime:
                stats_site.build(db_path, site, names)
                built["mtime"] = mtime

    refresh()

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):  # noqa: N802
            self._file(head=True)

        def do_GET(self):  # noqa: N802
            self._file(head=False)

        def _file(self, head: bool) -> None:
            rel = unquote(urlparse(self.path).path).lstrip("/") or "index.html"
            if rel == "latest.json":
                try:
                    refresh()
                except (OSError, sqlite3.Error) as e:
                    return self._send(500, json.dumps({"error": f"{db_path}: {e}"}).encode(), "application/json", head)
            path = (site / rel).resolve()
            if not path.is_file() or site.resolve() not in path.parents:
                return self._send(404, b"not found", "text/plain", head)
            data = path.read_bytes()
            kind = stats_site.CONTENT_TYPES.get(path.suffix, "application/octet-stream")
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
            if m and int(m.group(1)) < len(data):
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                return self._send(206, data[start:end + 1], kind, head,
                                  {"Content-Range": f"bytes {start}-{end}/{len(data)}"})
            self._send(200, data, kind, head)

        def _send(self, code: int, body: bytes, kind: str, head: bool = False,
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def log_message(self, *args):  # quiet: the worker makes many small range requests
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(db_path: str | Path, port: int = 8765, open_browser: bool = True,
          names: dict[str, str] | None = None, site: str | Path | None = None) -> None:
    server = make_server(db_path, port, names, site)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Season stats from {db_path} at {url} (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
