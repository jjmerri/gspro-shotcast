"""A problem report: one zip of everything the booth can gather on its own, for someone helping a
user who can't easily dig through folders. Logs, GSPro's log, health, the screens the booth left
behind, the hole maps it kept, the monitor layout and where GSPro's window is, and config.yaml with
anything secret blanked. The .env file (the API keys) never goes in: only its key names, and its
values are scrubbed out of every text file that does.

Player names and screenshots are in it, so whoever sends it should know that.
"""

from __future__ import annotations

import io
import json
import logging
import platform
import re
import secrets
import sqlite3
import time
import threading
import zipfile
from pathlib import Path

import yaml

from .config import Config

log = logging.getLogger(__name__)

LOG_DAYS = 3                  # booth and tray logs from the last few days
LOG_CAP = 4_000_000           # bytes kept from the end of each log
GSPRO_LOG_CAP = 3_000_000
FRAMES_DIR = "report-frames"  # under log_dir: the screens the booth leaves behind for a report
KEEP_REPORTS = 3
_building = threading.Lock()   # one report at a time: two clicks mustn't write the same zip
SECRET_KEY = re.compile(r"(password|secret|token(?!s)|api_?key|webhook|topic|_key$)", re.I)


def folder(cfg: Config) -> Path:
    return Path(cfg.log_dir) / "reports"


def read_env(path: Path) -> dict[str, str]:
    """KEY=value pairs, as dotenv.load_env reads them, without touching the environment."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def _redact(node):
    if isinstance(node, dict):
        return {k: "<redacted>" if SECRET_KEY.search(str(k)) and v not in (None, "") else _redact(v)
                for k, v in node.items()}
    if isinstance(node, list):
        return [_redact(v) for v in node]
    return node


def redact_config(text: str) -> str:
    """config.yaml with the value of any secret-looking key blanked, at any depth. It's parsed and
    written back (comments dropped) so no YAML form slips a value through; a file that won't parse
    is left out rather than sent raw."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)   # the message itself quotes the file: keep it out
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        return f"# config.yaml didn't parse{where}, so it was left out\n"
    return yaml.safe_dump(_redact(data), sort_keys=False, allow_unicode=True)


class Scrubber:
    """Blanks the .env's values out of text: every secret-looking key's value, and anything else long
    enough not to be an ordinary word or number. A short one only where it stands alone, so a two-letter
    topic doesn't blank every word with those letters in it."""

    def __init__(self, pairs) -> None:
        values = sorted({v for k, v in pairs if v and (len(v) >= 6 or SECRET_KEY.search(k))}, key=len, reverse=True)
        self.patterns = [re.compile(re.escape(v) if len(v) >= 6 else rf"(?<![\w-]){re.escape(v)}(?![\w-])")
                         for v in values]

    def __call__(self, text: str) -> str:
        for p in self.patterns:
            text = p.sub("<redacted>", text)
        return text


def env_files(config_path: Path) -> list[Path]:
    """Both .env files the booth loads (__main__._load_env): beside the code and beside config.yaml."""
    return list(dict.fromkeys((Path(__file__).resolve().parents[1] / ".env", config_path.resolve().parent / ".env")))


def tail(path: Path, cap: int) -> bytes:
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > cap:
            f.seek(size - cap)
            f.readline()   # start on a whole line
            return b"[... earlier lines left out ...]\n" + f.read()
        return f.read()


def _describe(path: Path) -> str:
    try:
        st = path.stat()
    except FileNotFoundError:
        return f"{path}: missing"
    except OSError as e:
        return f"{path}: can't look ({e.strerror or e!r})"
    return f"{path}: {st.st_size:,} bytes, modified {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}"


def screen_layout(cfg: Config) -> list[str]:
    """The monitors, the one the booth reads, and where GSPro's window is."""
    lines = [f"screen.capture: {cfg.screen.capture}   screen.monitor: {cfg.screen.monitor}"
             f"   screen.window_title: {cfg.screen.window_title!r}   screen.ocr: {cfg.screen.ocr}"]
    window = None
    try:
        from . import winshot   # first: it makes the process DPI-aware, so both report real pixels

        window = winshot.window_info(cfg.screen.window_title)
    except Exception as e:
        lines.append(f"GSPro window: couldn't look ({e!r})")
    monitors = []
    try:
        import mss

        with mss.mss() as sct:
            monitors = list(sct.monitors)
    except Exception as e:
        lines.append(f"Monitors: couldn't list them ({e!r})")
    for i, m in enumerate(monitors):
        if i:
            tag = " <- the booth reads this one" if i == cfg.screen.monitor and cfg.screen.capture == "monitor" else ""
            lines.append(f"Monitor {i}: {m['width']}x{m['height']} at ({m['left']}, {m['top']}){tag}")
    if monitors and cfg.screen.monitor >= len(monitors):
        lines.append(f"!! screen.monitor is {cfg.screen.monitor} but there are only {len(monitors) - 1} monitors")
    if window is None:
        lines.append(f"GSPro window: none found with {cfg.screen.window_title!r} in its title (not running?)")
    else:
        left, top, right, bottom = window["rect"]
        cx, cy = (left + right) // 2, (top + bottom) // 2
        on = next((i for i, m in enumerate(monitors) if i and m["left"] <= cx < m["left"] + m["width"]
                   and m["top"] <= cy < m["top"] + m["height"]), None)
        lines.append(f"GSPro window: {window['title']!r} {right - left}x{bottom - top} at ({left}, {top})"
                     f"{' MINIMIZED' if window['minimized'] else ''}, on monitor {on}")
        if cfg.screen.capture == "monitor" and on is not None and on != cfg.screen.monitor:
            lines.append(f"!! GSPro is on monitor {on} but the booth reads monitor {cfg.screen.monitor}")
    return lines


def _db_summary(db_path: Path, days: int = 14) -> tuple[list[str], list[tuple[str, bytes]]]:
    """Recent sessions' shot and map counts, and the hole maps kept lately."""
    if not db_path.exists():
        return [f"No season database at {db_path}"], []
    lines, images = [], []
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        since = time.time() - days * 86400
        rows = con.execute(
            "SELECT s.id, s.date, COUNT(h.id), GROUP_CONCAT(DISTINCT h.course),"
            " SUM(h.player IS NULL OR h.player = '' OR lower(h.player) = 'unknown' OR h.attributed = 0),"
            " GROUP_CONCAT(DISTINCT h.player)"
            " FROM sessions s JOIN shots h ON h.session_id = s.id WHERE s.started_at >= ?"
            " GROUP BY s.id ORDER BY s.id DESC LIMIT 15", (since,)).fetchall()
        lines.append(f"Sessions with shots in the last {days} days: {len(rows)}")
        for sid, date, shots, courses, unnamed, players in rows:
            maps = con.execute("SELECT COUNT(*), SUM(pin_x IS NOT NULL AND ball_x IS NOT NULL) FROM hole_images"
                               " WHERE session_id = ?", (sid,)).fetchone()
            lines.append(f"  session {sid} {date}: {shots} shots, {unnamed or 0} without a player,"
                         f" hole maps kept {maps[0]} (usable {maps[1] or 0}); {courses}; players: {players}")
        for course, hole, image, ball_x, pin_x, ts in con.execute(
                "SELECT course, hole, image, ball_x, pin_x, ts FROM hole_images ORDER BY ts DESC LIMIT 18"):
            ok = "usable" if ball_x is not None and pin_x is not None else "no-pin" if pin_x is None else "no-ball"
            name = re.sub(r"[^\w-]+", "_", f"{course}-{hole:02d}-{ok}")
            images.append((f"minimaps/{name}.jpg", image))
    finally:
        con.close()
    return lines, images


def _gspro_logs(cfg: Config) -> list[Path]:
    """GSPro's Unity log as configured, and its neighbours: newer GSPro builds (the beta) write
    Player.log instead of output_log.txt."""
    configured = Path(cfg.source.gspro_log).expanduser()
    names = [configured.name, "output_log.txt", "Player.log", "Player-prev.log"]
    return [configured.parent / n for n in dict.fromkeys(names)]


def build(cfg: Config, config_path: Path = Path("config.yaml")) -> Path:
    """Write the report zip into folder(cfg) and return its path."""
    with _building:
        return _build(cfg, config_path)


def _build(cfg: Config, config_path: Path) -> Path:
    from .updater import current_version

    log_dir = Path(cfg.log_dir)
    env: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    for p in env_files(config_path):
        found = read_env(p)
        env.update(found)
        pairs += found.items()   # both files' values, even one the other overrides
    scrub = Scrubber(pairs)
    out_dir = folder(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    # A random tail: the CLI and the tray can make one in the same second, in separate processes.
    path = out_dir / f"shotcast-report-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.zip"
    summary: list[str] = [
        f"Shotcast problem report, {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Shotcast {current_version()} on {platform.platform()}, Python {platform.python_version()}",
        "",
        "== Screen ==",
    ]
    errors: list[str] = []

    def guard(what: str, fn) -> None:
        try:
            fn()
        except Exception as e:
            log.exception("Problem report: %s failed", what)
            errors.append(f"{what}: {e!r}")

    tmp = path.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        def add_text(name: str, data: bytes) -> None:
            z.writestr(name, scrub(data.decode("utf-8", errors="replace")))

        def add_file(name: str, p: Path, cap: int | None = None) -> None:
            if p.is_file():
                if p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    z.write(p, name)
                else:
                    add_text(name, tail(p, cap) if cap else p.read_bytes())

        guard("screen layout", lambda: summary.extend(screen_layout(cfg)))

        summary += ["", "== GSPro =="]
        source = cfg.source
        summary.append(f"source.type: {source.type}")
        summary.append("Connector log " + _describe(Path(source.connector_log)))
        for p in _gspro_logs(cfg):
            summary.append("GSPro log " + _describe(p))
            guard(f"GSPro log {p.name}", lambda p=p: add_file(f"gspro/{p.name}", p, GSPRO_LOG_CAP))
        guard("connector log", lambda: add_file("gspro/ConnectDebug.txt", Path(source.connector_log), 1_000_000))

        summary += ["", "== Health =="]

        def health() -> None:
            hp = log_dir / "health.json"
            if not hp.exists():
                summary.append("No health.json yet")
                return
            data = json.loads(hp.read_text(encoding="utf-8"))
            summary.extend(f"fact {k}: {v}" for k, v in (data.get("facts") or {}).items())
            issues = data.get("issues") or []
            summary.append(f"Open issues: {len(issues)}")
            summary.extend(f"  [{i.get('level')}] {i.get('message')} (since {i.get('since')})" for i in issues)
            add_file("logs/health.json", hp)
        guard("health", health)
        guard("health history", lambda: add_file("logs/health-history.jsonl", log_dir / "health-history.jsonl", 500_000))

        summary += ["", "== Season database =="]

        def db() -> None:
            lines, images = _db_summary(Path(cfg.db_path))
            summary.extend(lines)
            for name, blob in images:
                z.writestr(name, blob)
        guard("season database", db)

        def logs() -> None:
            cutoff = time.time() - LOG_DAYS * 86400
            for pattern in ("booth-*.log", "tray-*.log"):
                for p in sorted(log_dir.glob(pattern)):
                    if p.name != "booth-errors.log" and p.stat().st_mtime >= cutoff:
                        add_file(f"logs/{p.name}", p, LOG_CAP)
            add_file("logs/booth-errors.log", log_dir / "booth-errors.log", 1_000_000)
            add_file("logs/live.json", log_dir / "live.json")
        guard("logs", logs)

        def screens() -> None:
            names = []
            for p in sorted(log_dir.glob("fixture-frame*")) + sorted(log_dir.glob("screen-check*.png")):
                add_file(f"screens/{p.name}", p)
                names.append(p.name)
            for p in sorted((log_dir / FRAMES_DIR).glob("*")):
                add_file(f"screens/{FRAMES_DIR}/{p.name}", p)
                names.append(f"{FRAMES_DIR}/{p.name}")
            shots = sorted((log_dir / "shots").glob("*/*.jpg"), key=lambda p: p.stat().st_mtime)[-12:]
            for p in shots:
                add_file(f"screens/shots/{p.parent.name}-{p.name}", p)
            summary.append(f"Screens left behind: {len(names) + len(shots)}"
                           f" ({', '.join(names) or 'none'}{f', {len(shots)} shot reviews' if shots else ''})")
        summary += ["", "== Screens =="]
        guard("screens", screens)

        def config() -> None:
            if config_path.is_file():
                add_text("config.yaml", redact_config(config_path.read_text(encoding="utf-8-sig")).encode())
            summary.append("Keys set in .env (values left out): " + (", ".join(sorted(env)) or "none"))
        summary += ["", "== Config =="]
        guard("config", config)

        if errors:
            summary += ["", "== Couldn't gather =="] + errors
        add_text("summary.txt", "\n".join(summary).encode())
    tmp.replace(path)
    def age(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    older = [p for p in sorted(out_dir.glob("shotcast-report-*.zip"), key=age) if p != path]
    for old in older[:max(0, len(older) - (KEEP_REPORTS - 1))]:
        try:
            old.unlink(missing_ok=True)
        except OSError:   # open in a zip viewer, say: tidy it next time
            log.warning("Couldn't remove the old report %s", old.name)
    return path


def keep_frame(log_dir: str | Path, reason: str, image, meta: dict, keep: int = 4) -> Path:
    """Save a screen the booth found something wrong with, for the next problem report: a full-size
    JPEG and a JSON note beside it, keeping the newest `keep` of each reason."""
    d = Path(log_dir) / FRAMES_DIR
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{reason}-{time.strftime('%Y%m%d-%H%M%S')}"
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=85)
    (d / f"{stem}.jpg").write_bytes(buf.getvalue())
    (d / f"{stem}.json").write_text(json.dumps({"captured": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason,
                                                "size": list(image.size), **meta}, default=str), encoding="utf-8")
    for old in sorted(d.glob(f"{reason}-*.jpg"))[:-keep]:
        try:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
        except OSError:
            log.warning("Couldn't remove the old screen %s", old.name)
    return d / f"{stem}.jpg"
