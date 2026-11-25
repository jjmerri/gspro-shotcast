"""The stats page as a static site: build it from the season database, and publish it to S3.

The heavy stats (season tables, strokes gained, scorecards, highlights) are computed here, once per
publish, into one JSON file per context. Shots and broadcast transcripts go into SQLite files (one
per year, below) that the page queries in the browser with sql.js-httpvfs, which reads only the pages a
query needs over HTTP range requests, so the page stays fast however long the season gets.

    index.html                      the page
    vendor/sql.js-httpvfs-<v>/      the browser SQLite library
    v/<version>/summary-<ctx>.json  ctx: all, league, tournament, practice
    v/<version>/player/<ctx>/<key>.json   one player's courses, highlights and records
    v/<version>/data.json           the version's data files (below)
    data/<hash>.db                  a year's shots, transcripts, hole replays and rounds' details
    latest.json                     {"version": ..., "data": ..., "page": ...}: which v/ the page reads
    manifest.webmanifest, sw.js, icons/   the home-screen app and its alerts (docs/tracking.md#alerts)

Each build is a new version, and latest.json is written last, so a page that's open while a new
round is published keeps reading the old files until it's refreshed.

The shots and transcripts are one file per year of play (data/<hash of the file>.db), listed newest
first in the version's data.json (and in latest.json, as "data"), each with the ranges of the rows in
it so the page only reads the files a query can find rows in. A year's file is built again only when
something in it changes (stats_cache, per year), and named by its content, so a publish uploads only
the files that changed, and an open page keeps the files it has read (immutable).

The summaries list every round, slim: what only a round's own page and its share card read
(ROUND_DETAILS) is in its year's file, read when the round is opened (docs/dev/round-details.md).
"""
from __future__ import annotations

import gc
import gzip
import hashlib
import json
import logging
import re
import secrets
import shutil
import sqlite3
import threading
import time
from html import escape
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from . import hole_replay
from . import highlights, record_clips
from . import season_analytics as A
from . import night_totals, stats_cache, stats_web
from .round_record import frozen

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("stats_web.html")
SITE_NAME, MORE_TAB = "Shotcast", "More"   # publish.site_name, publish.more_tab
VENDOR = Path(__file__).with_name("stats_vendor")
APP = Path(__file__).with_name("stats_app")   # the service worker and icons, at the site's root
CONTEXTS = {"all": None, "league": "league", "tournament": "tournament", "practice": "practice"}
SHOT_FILTERS = ("date", "player", "course", "club", "lie", "round")   # the page's exact-match filters
SHOT_RANGE = {"from": ">=", "to": "<="}                               # and its date range (YYYY-MM-DD)
PAGE_SIZE = 1024   # small pages: a lookup fetches little more than the rows it needs
APP_FILES = ("manifest.webmanifest", "sw.js")
KEEP_VERSIONS = 2  # the newest, plus the one before for pages opened just before a publish
DATA_FILE = re.compile(r"[0-9a-f]{20}\.db")   # a data file's name: its content's hash
# A summary round's fields only its own page reads: in its year's data file (pub_round), not the list.
ROUND_DETAILS = ("card", "summary", "tee", "tee_yards", "course_yards", "course_holes", "mulligans",
                 "putt_split", "scramble", "sand", "penalties", "longest_drive")
# latest.json's "page": the summaries' shape. A page open across a publish that changes it reloads.
PAGE_FORMAT = 2

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".json": "application/json", ".wasm": "application/wasm",
                 ".db": "application/vnd.sqlite3", ".txt": "text/plain; charset=utf-8",
                 ".png": "image/png", ".webmanifest": "application/manifest+json"}


def player_key(name: str) -> str:
    """A player's file name: stable, and safe whatever the name."""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def player_file(src: sqlite3.Connection, name: str, context: str | None,
                season: str | None = None) -> dict[str, Any]:
    """What the page shows for one picked player beyond the summary: their courses (hole by hole),
    highlights and records."""
    return {"name": name, "courses": A.courses(src, context, name),
            **stats_web.highlights_by_period(src, context, name, season=season)}


def new_version() -> str:
    """Sorts in build order (to the microsecond), so pruning keeps the newest."""
    now = time.time()
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f".{int(now % 1 * 1e6):06d}-" + secrets.token_hex(3)


def build_data_db(src: sqlite3.Connection, out: Path, names: dict[str, str] | None = None,
                  part: tuple[list[int], list[int]] | None = None,
                  rounds: list[tuple[int, int, str]] | None = None) -> None:
    """Shots (newest first, as the page lists them), their times, each session's transcript and each
    round's hole replays. `part`: only those of these sessions and rounds (page_parts' year). `rounds`:
    the rounds' details (id, round, JSON) that go in this file (round_details_rows)."""
    tmp, packed = out.with_name(out.name + ".tmp"), out.with_name(out.name + ".packed")
    tmp.unlink(missing_ok=True)
    packed.unlink(missing_ok=True)
    mine, args = "", ()
    if part is not None:
        mine, args = " AND session_id IN (SELECT value FROM json_each(?))", (json.dumps(list(part[0])),)
    shots = A.shot_log(src, None, limit=-1, sessions=None if part is None else part[0])
    kind = {r[0]: "league" if r[1] else "tournament" if r[2] else "practice"
            for r in src.execute("SELECT id, is_league, is_tournament FROM shots WHERE 1=1" + mine, args)}
    round_of = stats_web.shot_rounds(src)
    sessions = [r[0] for r in src.execute("SELECT DISTINCT session_id FROM lines WHERE session_id IS NOT NULL"
                                          + mine + " ORDER BY session_id", args)]
    with closing(sqlite3.connect(tmp)) as db:
        db.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        db.execute("PRAGMA journal_mode = DELETE")
        db.executescript("""
            CREATE TABLE pub_shot (ord INTEGER PRIMARY KEY, id INTEGER, kind TEXT, date TEXT,
                                   player TEXT, course TEXT, club TEXT, lie TEXT, round INTEGER);
            CREATE TABLE pub_shot_json (ord INTEGER PRIMARY KEY, json TEXT NOT NULL);
            CREATE TABLE pub_transcript (session_id INTEGER PRIMARY KEY, json TEXT NOT NULL);
            CREATE TABLE pub_replay (round INTEGER, hole INTEGER, json TEXT NOT NULL, PRIMARY KEY (round, hole));
            CREATE TABLE pub_hole_image (id INTEGER PRIMARY KEY, src TEXT NOT NULL);
            CREATE TABLE pub_shot_ts (ts REAL PRIMARY KEY);
            CREATE TABLE pub_round (id INTEGER PRIMARY KEY, round INTEGER NOT NULL, json TEXT NOT NULL);
            CREATE INDEX pub_round_round ON pub_round (round);
        """)
        db.executemany("INSERT INTO pub_round VALUES (?, ?, ?)", rounds or [])
        # Every swing's time (a mulligan's too: it's on the stream) in the published rounds, for a replay's
        # Next shot button.
        db.executemany("INSERT OR IGNORE INTO pub_shot_ts VALUES (?)", src.execute(
            "SELECT ts FROM shots WHERE ts IS NOT NULL AND COALESCE(outcome, '') != 'gimme'" + mine, args))
        db.executemany("INSERT INTO pub_shot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [(i, s["id"], kind.get(s["id"]), s["date"], s["player"], s["course"], s["club"],
                         s["lie_before"], round_of.get(s["id"])) for i, s in enumerate(shots)])
        db.executemany("INSERT INTO pub_shot_json VALUES (?, ?)",
                       [(i, json.dumps(s, separators=(",", ":"))) for i, s in enumerate(shots)])
        db.executemany("INSERT INTO pub_transcript VALUES (?, ?)",
                       [(sid, json.dumps(A.transcript(src, sid, names), separators=(",", ":")))
                        for sid in sessions])
        replays = hole_replay.hole_replays(src, None if part is None else part[1])
        db.executemany("INSERT INTO pub_replay VALUES (?, ?, ?)",
                       [(rnd, hole, json.dumps(rp, separators=(",", ":"))) for (rnd, hole), rp in replays.items()])
        # Each hole's minimap once, however many rounds' replays draw on it.
        db.executemany("INSERT INTO pub_hole_image VALUES (?, ?)", hole_replay.hole_image_sources(
            src, {rp["map"]["id"] for rp in replays.values() if "map" in rp}).items())
        # Each filter walks its own index in the page's order, so the newest matches come first.
        for col in (*SHOT_FILTERS, "kind"):
            db.execute(f"CREATE INDEX pub_shot_{col} ON pub_shot ({col}, ord)")
        db.execute("CREATE INDEX pub_shot_id ON pub_shot (id)")   # a link to one shot finds its round
        db.execute("ANALYZE")   # lets SQLite pick the most selective filter's index
        db.commit()
        # Packed into a new file (each table's pages together, for the page's range reads): as VACUUM
        # does, without its copy back into this one.
        db.execute("VACUUM INTO ?", (str(packed),))
    tmp.unlink()
    packed.replace(out)


def page_parts(src: sqlite3.Connection) -> dict[str, tuple[list[int], list[int]]]:
    """The page's data split by year of play, newest first: each year's sessions (by their date) and the
    rounds whose hole replays go with them (by their first hole's date). Rows of a session that isn't on
    file (none of their own date) go with the newest year."""
    year_of = {sid: d[:4] for d, sids in night_totals.nights(src).items() for sid in sids}
    seen = {r[0] for r in src.execute("SELECT DISTINCT session_id FROM shots WHERE session_id IS NOT NULL"
                                      " UNION SELECT DISTINCT session_id FROM lines WHERE session_id IS NOT NULL")}
    loose = sorted(seen - year_of.keys())
    years = sorted(set(year_of.values()), reverse=True)
    if loose and not years:
        years = [""]
    parts: dict[str, tuple[list[int], list[int]]] = {y: ([], []) for y in years}
    for sid, y in year_of.items():
        parts[y][0].append(sid)
    if loose:
        parts[years[0]][0].extend(loose)
    for rnd, (_, sessions) in hole_replay.replay_rounds(src).items():
        placed = [year_of[s] for s in sessions if s in year_of]
        parts[min(placed) if placed else years[0]][1].append(rnd)
    for sessions, rounds in parts.values():
        sessions.sort()
        rounds.sort()
    return parts


def _year_sigs(src: sqlite3.Connection, parts: dict[str, tuple[list[int], list[int]]],
               names: dict[str, str] | None) -> dict[str, str]:
    """Each year's signature: its nights' (their sessions and the records played in them), the records of
    a line in it about a shot in another year (or the other way about), its rounds' hole replays', the
    records with rows in a session that isn't on file (the newest year's), and the speakers' names."""
    gens = stats_cache.record_sigs(src)
    year_of = {sid: y for y, (sessions, _) in parts.items() for sid in sessions}
    nights = night_totals.night_sigs(src)
    per: dict[str, set] = {y: set() for y in parts}
    for ses_a, rec_a, ses_b, rec_b in src.execute(
            "SELECT DISTINCT l.session_id, l.record_uuid, s.session_id, s.record_uuid FROM main.lines l"
            " JOIN main.shots s ON s.id = l.shot_id WHERE s.session_id IS NOT l.session_id"):
        for ses in (ses_a, ses_b):
            if ses in year_of:
                per[year_of[ses]] |= {rec_a or "", rec_b or ""}
    dated = {sid for sids in night_totals.nights(src).values() for sid in sids}
    if parts:
        newest = next(iter(parts))
        for uuid, sessions in night_totals.record_sessions(src).items():
            if any(s not in dated for s in sessions):
                per[newest].add(uuid or "")
    replays = hole_replay.replay_rounds(src)
    by_year: dict[str, list] = {y: [] for y in parts}
    for d, s in nights.items():
        if d[:4] in by_year:
            by_year[d[:4]].append((d, s))
    return {y: stats_cache.sig(y, by_year[y], sorted((u, gens.get(u)) for u in per[y]),
                               [(r, replays[r][0]) for r in rounds], sorted((names or {}).items()))
            for y, (_, rounds) in parts.items()}


def _year_file(src: sqlite3.Connection, site: Path, year: str, part: tuple[list[int], list[int]],
               names: dict[str, str] | None, rounds: list[tuple[int, int, str]] | None = None) -> dict[str, Any]:
    """Build one year's data file into site/data, named by its content; returns its data.json entry."""
    ddir = site / "data"
    ddir.mkdir(parents=True, exist_ok=True)
    tmp = ddir / f"building-{year or 'none'}-{secrets.token_hex(4)}.db.tmp"
    build_data_db(src, tmp, names, part, rounds)
    h = hashlib.sha256()
    with tmp.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    file = f"data/{h.hexdigest()[:20]}.db"
    with closing(sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)) as db:
        def span(sql: str) -> list | None:
            lo, hi = db.execute(sql).fetchone()
            return None if lo is None else [lo, hi]
        # (its swings' times, and its shots': newest first, so the first and last)
        ts = [t for t in (span("SELECT MIN(ts), MAX(ts) FROM pub_shot_ts"), span(
            "SELECT MIN(t), MAX(t) FROM (SELECT json_extract(json, '$.ts') AS t FROM pub_shot_json"
            " WHERE ord IN (0, (SELECT MAX(ord) FROM pub_shot_json)))")) if t]
        ts = [min(t[0] for t in ts), max(t[1] for t in ts)] if ts else None
        ids = span("SELECT MIN(id), MAX(id) FROM pub_shot")
        got = [x for x in (span(f"SELECT MIN(round), MAX(round) FROM {t}") for t in ("pub_shot", "pub_replay", "pub_round")) if x]
        rounds = [min(x[0] for x in got), max(x[1] for x in got)] if got else None
    sessions = [part[0][0], part[0][-1]] if part[0] else None
    if (site / file).exists():
        tmp.unlink()
    else:
        tmp.replace(site / file)
    return {"year": year, "file": file, "ts": ts, "sessions": sessions, "rounds": rounds, "ids": ids}


def split_rounds(name: str, body: dict[str, Any], details: dict[int, dict[str, Any]]) -> None:
    """Take each round's ROUND_DETAILS out of a summary's rounds into `details` (id -> {"round", "date",
    "row"}), every context's in CONTEXTS' order. The "all" summary's, which lists every round, are each
    round's row; another context's that differ (strokes gained from its own shots) go under the row's
    "ctx". A summary's `vs` figures are kept without the season's, which the page takes from its players."""
    for r in body["rounds"]:
        mine = {k: r.pop(k) for k in ROUND_DETAILS if k in r}
        if mine.get("summary"):
            mine["summary"] = {**mine["summary"], "vs": [{k: v for k, v in x.items() if k != "season"}
                                                         for x in mine["summary"]["vs"]]}
        got = details.get(r["id"])
        if got is None:
            details[r["id"]] = {"round": r["round"], "date": r["date"], "row": mine}
            continue
        row = got["row"]
        diff = {k: mine.get(k) for k in ROUND_DETAILS if mine.get(k) != row.get(k)}
        if diff:
            row.setdefault("ctx", {})[name] = diff


def _round_rows(parts: dict[str, tuple[list[int], list[int]]],
                details: dict[int, dict[str, Any]]) -> dict[str, list[tuple[int, int, str]]]:
    """Each year's rounds' details, as pub_round rows (id, round, JSON): a round's in the year with its
    hole replays, else its date's, else the newest. Keys keep the build's order (the page lists a round's
    mulligans and the like as they come); it's the same every build."""
    year_of = {rnd: y for y, (_, rounds) in parts.items() for rnd in rounds}
    out: dict[str, list[tuple[int, int, str]]] = {y: [] for y in parts}
    for i, d in sorted(details.items()):
        y = year_of.get(d["round"]) or ((d["date"] or "")[:4] if (d["date"] or "")[:4] in parts else next(iter(parts)))
        out[y].append((i, d["round"], json.dumps(d["row"], separators=(",", ":"))))
    return out


def round_details(site: str | Path, rnd: int) -> dict[int, dict[str, Any]]:
    """One round's details (pub_round: each side's row, by id) from the site's latest version's data files."""
    site = Path(site)
    for e in json.loads((site / "latest.json").read_text(encoding="utf-8")).get("data") or []:
        span = e.get("rounds")
        if not span or not span[0] <= rnd <= span[1]:
            continue
        try:
            with closing(sqlite3.connect(f"file:{site / e['file']}?mode=ro", uri=True)) as db:
                got = {i: json.loads(j) for i, j in db.execute("SELECT id, json FROM pub_round WHERE round = ?", (rnd,))}
        except sqlite3.Error:
            continue   # (a file from before pub_round)
        if got:
            return got
    return {}


def page_files(src: sqlite3.Connection, site: Path, names: dict[str, str] | None = None,
               details: dict[int, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The page's data files, newest year first (data.json): each year's kept from the last build while
    nothing in it has changed (and its file is still in this site), else built again. `details`: the
    rounds' (split_rounds)."""
    parts = page_parts(src)
    if details and not parts:
        parts = {"": ([], [])}
    rows = _round_rows(parts, details or {})
    cache = stats_cache.of(src)
    kept: dict = {}
    sigs: dict[str, str] = {}
    kind = "page_year:" + stats_cache.sig(str(site.resolve()))   # each site (the PC's, the published) its own files
    if cache is not None:
        sigs = {y: stats_cache.sig(s, hashlib.sha256("\n".join(j for _, _, j in rows[y]).encode()).hexdigest())
                for y, s in _year_sigs(src, parts, names).items()}
        kept = cache.get(kind, sigs)
    out, new = [], {}
    for year, part in parts.items():
        entry = kept.get(year)
        if entry is None or not (site / entry["file"]).is_file():
            entry = _year_file(src, site, year, part, names, rows[year])
            new[year] = (sigs.get(year, ""), entry)
        out.append(entry)
    if cache is not None:
        cache.hits += len(parts) - len(new)
        cache.misses += len(new)
        cache.put(kind, new, set(parts))
    return out


def _referenced(site: Path, versions: list[str]) -> set[str] | None:
    """The data files these versions read (a version from before data files reads none); None while one
    is still being built (no data.json yet, nor a stats.db of its own)."""
    out: set[str] = set()
    for v in versions:
        try:
            out |= {e["file"] for e in json.loads((site / "v" / v / "data.json").read_text(encoding="utf-8"))}
        except FileNotFoundError:
            if not (site / "v" / v / "stats.db").exists():
                return None
    return out


def shot_query(context: str | None, filters: dict[str, str], limit: int = 500) -> tuple[str, list]:
    """The page's shot query (stats_web.html builds the same SQL): newest first, up to `limit`."""
    where, args = [], []
    if context in ("league", "tournament", "practice"):
        where.append("s.kind = ?")
        args.append(context)
    for key in SHOT_FILTERS:
        if filters.get(key):
            where.append(f"s.{key} = ?")
            args.append(int(filters[key]) if key == "round" else filters[key])
    for key, op in SHOT_RANGE.items():
        if filters.get(key):
            where.append(f"s.date {op} ?")
            args.append(filters[key])
    sql = ("SELECT j.json FROM pub_shot s JOIN pub_shot_json j ON j.ord = s.ord"
           + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY s.ord LIMIT {int(limit)}")
    return sql, args


def _note_records(out: dict[int, dict], by_period: dict[str, dict]) -> None:
    for rec in by_period.values():
        for shot in stats_web.record_shots(rec):
            out[shot["id"]] = shot


# publish.theme's names for the page's CSS colours, and the page's own values for them.
THEME_VARS = {"background": "--bg", "panel": "--panel", "border": "--line", "text": "--text", "muted": "--dim",
              "accent": "--accent"}
SHIPPED = {"--bg": "#0f1715", "--panel": "#16211e", "--line": "#24332f", "--text": "#e6efe9", "--dim": "#8fa59b",
           "--accent": "#5fc58a"}
# The page's other shades, mixed from those: (colour, share of it, over this one).
MIXED = {"--raised": ("--accent", 0.12, "--bg"), "--hover": ("--text", 0.07, "--bg"), "--deep": ("#000000", 0.25, "--bg"),
         "--plot": ("--panel", 0.5, "--bg"), "--track": ("--accent", 0.25, "--panel"), "--soft": ("--text", 0.75, "--dim")}
HEX = re.compile(r"#(?:[0-9a-f]{3}|[0-9a-f]{6})")


def _rgb(colour: str) -> tuple[int, int, int]:
    h = colour.lstrip("#")
    h = "".join(c * 2 for c in h) if len(h) == 3 else h
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: str, share: float, b: str) -> str:
    return "#" + "".join(f"{round(x * share + y * (1 - share)):02x}" for x, y in zip(_rgb(a), _rgb(b)))


def theme_css(theme: dict[str, str]) -> str:
    """publish.theme (ThemeConfig.colors()) as CSS variables over the page's own; empty without one.
    Every value is a plain colour worked out here, so any browser (and the share image's canvas) takes it."""
    theme = {k: v for k, v in (theme or {}).items() if k in THEME_VARS or k == "highlight"}
    if not theme:
        return ""
    for v in theme.values():   # only ever a colour in the page's <style>
        if not HEX.fullmatch(v):
            raise ValueError(f"not a theme colour: {v!r}")
    base = {**SHIPPED, **{THEME_VARS[k]: v for k, v in theme.items() if k in THEME_VARS}}
    css = {THEME_VARS[k]: v for k, v in theme.items() if k in THEME_VARS}
    css.update({name: _mix(base.get(a, a), share, base[b]) for name, (a, share, b) in MIXED.items()})
    if "highlight" in theme:
        css.update({"--raised": theme["highlight"], "--hover": theme["highlight"], "--deep": theme["highlight"]})
    css["--scrim"] = "rgba({}, {}, {}, .92)".format(*_rgb(base["--bg"]))   # a recap over the video
    r, g, b = (c / 255 for c in _rgb(base["--accent"]))
    light = 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4
    css["--on-accent"] = "#000" if light else "#fff"   # text on accent
    css["--glow"] = base["--accent"] if light else SHIPPED["--accent"]   # accent on the dark video overlays
    return ":root { " + " ".join(f"{k}:{v};" for k, v in css.items()) + " }"


def page(site_name: str = SITE_NAME, more_tab: str = MORE_TAB, theme: dict[str, str] | None = None) -> str:
    """The page, with the site's name (title and header), its last section's tab name and its colours
    (publish.theme). Without a theme, the page exactly as it ships."""
    html = PAGE.read_text(encoding="utf-8")
    swaps = [("<title>Shotcast</title>", f"<title>{escape(site_name)}</title>"),
             ('<span id="sitename">Shotcast</span>', f'<span id="sitename">{escape(site_name)}</span>'),
             ('<meta name="more-tab" content="More">', f'<meta name="more-tab" content="{escape(more_tab)}">')]
    if css := theme_css(theme or {}):
        swaps += [('<meta name="theme-color" content="#0f1715">',
                   f'<meta name="theme-color" content="{(theme or {}).get("background", "#0f1715")}">'),
                  ("</style>\n</head>", f"</style>\n<style id=\"theme\">\n  {css}\n</style>\n</head>")]
    for old, new in swaps:
        assert html.count(old) == 1, old
        html = html.replace(old, new)
    return html


def manifest(site_name: str = SITE_NAME, theme: dict[str, str] | None = None) -> str:
    """The home-screen app's manifest: the page, standalone, with the logo."""
    icons = [{"src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
             {"src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
             {"src": "icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]
    bg = (theme or {}).get("background") or "#0f1715"
    return json.dumps({"name": site_name, "short_name": site_name, "start_url": "./", "scope": "./",
                       "display": "standalone", "background_color": bg, "theme_color": bg,
                       "icons": icons}, indent=1)


_gc_lock = threading.Lock()
_gc_builds = 0       # builds under way (the page's and a publish can overlap)
_gc_normal = None    # the collector's thresholds from before the first build, while raised
_gc_timer = None
GC_BUILD_THRESHOLDS = (100_000, 50, 10)
GC_TIME_LIMIT = 600  # seconds; a build still going after this gets the usual collector back


def _gc_restore():
    global _gc_normal, _gc_timer
    if _gc_normal is not None:
        gc.set_threshold(*_gc_normal)
        _gc_normal = None
    if _gc_timer is not None:
        _gc_timer.cancel()
        _gc_timer = None


def _gc_time_up(timer):
    with _gc_lock:
        if timer is _gc_timer:
            _gc_restore()


@contextmanager
def gc_paused():
    """Python's cycle collector runs rarely while a build runs. A build holds a great many objects
    and makes next to no cycles (its peak memory is the same without the collector), so every full
    collection went over all of them for nothing: a quarter of a build's time. The young
    collections still run now and then, so cycles made meanwhile (by the booth's thread, say) are
    still freed. The usual thresholds come back when the last build ends, or after GC_TIME_LIMIT."""
    global _gc_builds, _gc_normal, _gc_timer
    with _gc_lock:
        if _gc_builds == 0:
            _gc_normal = gc.get_threshold()
            gc.set_threshold(*GC_BUILD_THRESHOLDS)
            _gc_timer = threading.Timer(GC_TIME_LIMIT, _gc_time_up)
            _gc_timer.args = (_gc_timer,)
            _gc_timer.daemon = True
            try:
                _gc_timer.start()
            except BaseException:
                _gc_timer = None
                _gc_restore()
                raise
        _gc_builds += 1
    try:
        yield
    finally:
        with _gc_lock:
            _gc_builds -= 1
            if _gc_builds == 0:
                _gc_restore()


def build(db_path: str | Path, site: str | Path, names: dict[str, str] | None = None,
          records: dict[int, dict] | None = None, site_name: str = SITE_NAME, more_tab: str = MORE_TAB,
          local: bool = False, season_start: str | None = None, reels: dict[int, dict] | None = None,
          news: bool = True, theme: dict[str, str] | None = None, league_file: str | Path | None = None) -> str:
    """Build (or refresh) the site in `site` from the season database; returns the new version. With
    `records`, it's filled with every shot holding a record (id -> the record), for record_clips; with
    `reels`, with every round's highlight moments (round -> its reel), for highlights.sync.
    `site_name` and `more_tab` are publish.site_name and publish.more_tab. `local`: for the page served
    on the PC (stats_web.make_server), which also lists the rounds excluded from the stats.
    `season_start`: publish.season_start (MM-DD), for the records' This season range. `news`: False
    leaves out the page's What's new card (the demo, whose visitors aren't following the releases).
    `theme`: publish.theme's colours (ThemeConfig.colors()). `league_file`: the league file, whose
    names for the courses and players the page shows (stats_web.league_names); one that won't load is
    skipped, the page going by the names as stored."""
    records = {} if records is None else records
    league = None
    if league_file:
        from .league import League

        try:
            league = League.load(league_file)
        except Exception as e:
            print(f"League file not read, the stats go by the names as stored: {e}")
    db_path, site = Path(db_path), Path(site)
    version = new_version()
    vdir = site / "v" / version
    vdir.mkdir(parents=True, exist_ok=True)
    details: dict[int, dict[str, Any]] = {}
    with gc_paused(), closing(stats_web.connect(db_path, league)) as src, frozen(src), stats_cache.opened(src, db_path):
        for name, context in CONTEXTS.items():
            body: dict[str, Any] = stats_web.summary(src, context, names, local, season_start)
            if not news:
                body["news"] = []
            body["db"] = {"name": db_path.name, "modified": stats_web.modified(db_path), "published": time.time()}
            pdir = vdir / "player" / name
            pdir.mkdir(parents=True, exist_ok=True)
            _note_records(records, body["records"])
            for p in body["players"]:
                p["file"] = player_key(p["name"])
                pf = player_file(src, p["name"], context, season_start)
                _note_records(records, pf["records"])
                (pdir / f"{p['file']}.json").write_text(json.dumps(pf, separators=(",", ":")), encoding="utf-8")
            split_rounds(name, body, details)
            (vdir / f"summary-{name}.json").write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        data = page_files(src, site, names, details)
        (vdir / "data.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        if reels is not None:
            reels.update(highlights.rounds(src))
    (site / "index.html").write_text(page(site_name, more_tab, theme), encoding="utf-8")
    (site / "manifest.webmanifest").write_text(manifest(site_name, theme), encoding="utf-8")
    shutil.copytree(APP, site, dirs_exist_ok=True)
    for lib in VENDOR.iterdir():
        if lib.is_dir():
            shutil.copytree(lib, site / "vendor" / lib.name, dirs_exist_ok=True)
    tmp = site / "latest.json.tmp"
    tmp.write_text(json.dumps({"version": version, "data": data, "page": PAGE_FORMAT}), encoding="utf-8")
    tmp.replace(site / "latest.json")
    for old in sorted((site / "v").iterdir(), key=lambda p: p.name)[:-KEEP_VERSIONS]:
        if old.name != version:
            shutil.rmtree(old, ignore_errors=True)
    used = _referenced(site, [p.name for p in (site / "v").iterdir()])
    for f in (site / "data").glob("*") if used is not None else ():
        if (DATA_FILE.fullmatch(f.name) and f"data/{f.name}" not in used
                or f.name.startswith("building-") and f.stat().st_mtime < time.time() - 86400):   # (a crashed build's)
            f.unlink(missing_ok=True)
    return version


# ---- S3 ---------------------------------------------------------------------------------------

def _cache_control(key: str) -> str:
    if key.startswith(("v/", "vendor/", "data/")):
        return "public, max-age=31536000, immutable"   # a version's files never change, nor a data file
    return "no-cache"                                   # index.html, latest.json: always revalidate


def upload(site: str | Path, bucket: str, prefix: str = "", client: Any = None) -> str:
    """Upload the built site to s3://bucket/prefix: the data files the bucket doesn't have yet, the new
    version's files (its JSON stored gzipped) and the page first, latest.json last (so nobody is pointed at files that aren't
    there yet), then drop all but the newest KEEP_VERSIONS versions, and the data files none of them
    reads. Returns the published version."""
    site = Path(site)
    if client is None:
        from . import backend

        client = backend.s3()
    prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
    latest = (site / "latest.json").read_bytes()
    version = json.loads(latest)["version"]
    data = {e["file"] for e in json.loads(latest).get("data", [])}
    files = [p for p in site.rglob("*") if p.is_file() and not p.name.endswith(".tmp")]
    rel = lambda p: p.relative_to(site).as_posix()   # noqa: E731
    keep = {f"v/{version}/"}
    order = ([p for p in files if rel(p) in data]
             + [p for p in files if rel(p).startswith(f"v/{version}/")]
             + [p for p in files if rel(p).startswith("vendor/")]
             + [p for p in files if rel(p) in APP_FILES or rel(p).startswith("icons/")]
             + [site / "index.html"])
    existing = _list(client, bucket, prefix)
    for path in order:
        key = rel(path)
        if key.startswith(("vendor/", "data/")) and prefix + key in existing:
            continue   # vendored files are versioned by their folder name, data files named by their content
        if key.startswith("v/") and path.suffix == ".json":
            # Stored gzipped: CloudFront compresses only files up to 10 MB, and a summary passes that
            # after a few years (25 MB at 10 years, 2.6 MB gzipped).
            client.put_object(Bucket=bucket, Key=prefix + key, Body=gzip.compress(path.read_bytes(), 6, mtime=0),
                              ContentType=CONTENT_TYPES[".json"], ContentEncoding="gzip",
                              CacheControl=_cache_control(key))
            continue
        client.upload_file(str(path), bucket, prefix + key, ExtraArgs={
            "ContentType": CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
            "CacheControl": _cache_control(key)})
    client.put_object(Bucket=bucket, Key=prefix + "latest.json", Body=latest,
                      ContentType=CONTENT_TYPES[".json"], CacheControl=_cache_control("latest.json"))
    versions = sorted({k[len(prefix) + 2:].split("/", 1)[0] for k in existing if k.startswith(prefix + "v/")}
                      | {version})
    keep |= {f"v/{v}/" for v in versions[-KEEP_VERSIONS:]}   # `keep` already has this one
    stale = [k for k in existing if k.startswith(prefix + "v/") and not any(k.startswith(prefix + d) for d in keep)]
    used = _bucket_referenced(site, client, bucket, prefix, existing, [d[2:-1] for d in keep])
    if used is not None:
        stale += [k for k in existing if k.startswith(prefix + "data/") and k[len(prefix):] not in used | data]
    for chunk in range(0, len(stale), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[chunk:chunk + 1000]]})
    return version


def _bucket_referenced(site: Path, client: Any, bucket: str, prefix: str, existing: set[str],
                       versions: list[str]) -> set[str] | None:
    """The data files these versions in the bucket read (the site's copy of their data.json, else the
    bucket's); None if one can't be read (nothing's pruned then)."""
    out: set[str] = set()
    for v in versions:
        key = f"v/{v}/data.json"
        try:
            if (site / key).is_file():
                body = (site / key).read_bytes()
            elif prefix + key in existing:
                body = client.get_object(Bucket=bucket, Key=prefix + key)["Body"].read()
                if body[:2] == b"\x1f\x8b":   # (stored gzipped)
                    body = gzip.decompress(body)
            else:
                continue   # a version from before data files, or the one just uploaded (`data`)
            out |= {e["file"] for e in json.loads(body)}
        except Exception as e:   # (pruned next time)
            log.warning("data files not pruned: %s: %s", key, e)
            return None
    return out


def _list(client: Any, bucket: str, prefix: str) -> set[str]:
    keys: set[str] = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys |= {o["Key"] for o in page.get("Contents", [])}
    return keys


def publish(db_path: str | Path, site: str | Path, bucket: str, prefix: str = "", region: str | None = None,
            names: dict[str, str] | None = None, site_name: str = SITE_NAME, more_tab: str = MORE_TAB,
            season_start: str | None = None, theme: dict[str, str] | None = None,
            league_file: str | Path | None = None) -> str:
    """Build the site from the database and upload it, then keep the record shots' clips and the
    rounds' highlight reels up to date (record_clips.py, highlights.py). Returns the published version."""
    from . import backend

    records: dict[int, dict] = {}
    reels: dict[int, dict] = {}
    build(db_path, site, names, records, site_name, more_tab, season_start=season_start, reels=reels, theme=theme,
          league_file=league_file)
    client = backend.s3(region)
    version = upload(site, bucket, prefix, client)
    try:
        n = record_clips.sync(client, bucket, prefix, records)
        print(f"Record clips: {n['kept']} kept, {n['added']} new, {n['removed']} removed")
    except Exception as e:   # the stats are up: the clips are tried again next publish
        print(f"Record clips not updated: {e}")
    try:
        n = highlights.sync(client, bucket, prefix, reels, stats_web.season_start(season_start))
        print(f"Highlight reels: {n['kept']} kept, {n['added']} new, {n['removed']} removed")
    except Exception as e:   # ...and the reels
        print(f"Highlight reels not updated: {e}")
    return version


def site_dir(cfg: Any) -> Path:
    """Where the booth builds the site before uploading it."""
    return Path(cfg.log_dir) / "stats-site"


def publish_command(cfg: Any) -> list[str] | None:
    """The command that publishes the site for this config (the booth after a round, the tray after
    an edit on the local page), or None without a bucket."""
    import sys

    p = cfg.publish
    if not p.bucket:
        return None
    names = {key: persona.name for key, persona in cfg.personas.items()}
    return [sys.executable, "-m", "commentator.stats_site", "--db", str(cfg.db_path),
            "--site", str(site_dir(cfg)), "--bucket", p.bucket, "--prefix", p.prefix,
            "--names", json.dumps(names), "--site-name", p.site_name, "--more-tab", p.more_tab] \
        + (["--region", p.region] if p.region else []) \
        + (["--season-start", p.season_start] if p.season_start else []) \
        + (["--theme", json.dumps(theme)] if (theme := p.theme.colors()) else []) \
        + (["--league", str(cfg.league_file)] if cfg.league_file else [])


@contextmanager
def site_lock(site: str | Path, timeout: float = 300.0):
    """One publish at a time, across processes (the booth's after a round, the tray's after an edit):
    they build into the same folder and upload over each other. Each builds from the database once it
    holds the lock, so the last to finish has the newest data."""
    import sys

    path = Path(str(site).rstrip("/\\") + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with open(path, "a+b") as f:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"another publish has held {path} for {timeout:.0f}s") from None
                time.sleep(0.5)
        yield   # closing the file releases the lock


def main(argv: list[str] | None = None) -> None:
    """`python -m commentator.stats_site`: what the booth runs, in its own process, after a round."""
    import argparse

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--prefix", default="")
    p.add_argument("--region")
    p.add_argument("--names", default="{}", help="persona keys to on-air names, as JSON")
    p.add_argument("--site-name", default=SITE_NAME)
    p.add_argument("--more-tab", default=MORE_TAB)
    p.add_argument("--season-start", help="the league's season start, MM-DD")
    p.add_argument("--theme", default="{}", help="publish.theme's colours, as JSON")
    p.add_argument("--league", help="the league file, for its names for the courses and players")
    a = p.parse_args(argv)
    with site_lock(a.site):
        version = publish(a.db, a.site, a.bucket, a.prefix, a.region, json.loads(a.names), a.site_name,
                          a.more_tab, a.season_start, theme=json.loads(a.theme), league_file=a.league)
    print(f"Published {version} to s3://{a.bucket}/{a.prefix}")


if __name__ == "__main__":
    main()
