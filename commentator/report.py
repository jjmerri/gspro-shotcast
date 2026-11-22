"""A problem report: one zip of everything the booth can gather on its own, for someone helping a
user who can't easily dig through folders. Logs, GSPro's log, health, the screens the booth left
behind, the hole maps it kept, the monitor layout and where GSPro's window is, the shot data GSPro
and the launch monitor's own software keep (to learn a monitor we haven't seen), and config.yaml with
anything secret blanked. The .env file (the API keys) never goes in: only its key names, and its
values are scrubbed out of every text file that does. GSPro's and the launch monitor's files also
lose anything that looks like a sign-in (emails, tokens, passwords, keys).

Player names and screenshots are in it, so whoever sends it should know that.
"""

from __future__ import annotations

import io
import json
import logging
import platform
import re
import os
import secrets
import sqlite3
import subprocess
import time
import threading
import zipfile
from pathlib import Path

import yaml

from . import perf_log, prompt_log
from .config import Config

log = logging.getLogger(__name__)

LOG_DAYS = 3                  # booth and tray logs from the last few days
LOG_CAP = 4_000_000           # bytes kept from the end of each log
GSPRO_LOG_CAP = 3_000_000
PERF_LOG_CAP = 2_000_000      # per day of the perf log (perf_log), from the newest
PROMPT_LOG_CAP = 8_000_000    # per day of model prompts (prompt_log), from the newest: ~1 MB zipped
ROUND_FILES = 3
ROUND_FILE_CAP = 2_000_000               # the newest round files kept from round ends (round_file.archive)
FRAMES_DIR = "report-frames"  # under log_dir: the screens the booth leaves behind for a report
KEEP_REPORTS = 3
_building = threading.Lock()   # one report at a time: two clicks mustn't write the same zip
SECRET_KEY = re.compile(r"(password|secret|token(?!s)|api_?key|webhook|topic|_key$)", re.I)


def folder(cfg: Config) -> Path:
    return Path(cfg.log_dir) / "reports"


def env_pairs(path: Path) -> list[tuple[str, str]]:
    """Every KEY=value line dotenv.load_env would set, in order (a key set twice is here twice), without
    touching the environment. A blank value sets nothing, so it's left out."""
    out: list[tuple[str, str]] = []
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            out.append((key, value))
    return out


def read_env(path: Path) -> dict[str, str]:
    """The KEY=value pairs dotenv.load_env would leave set, without touching the environment."""
    return dict(env_pairs(path))


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


_PROFILES_DIR = re.compile(r"(?i)(?<!\\)(\\+|/)Users(?=[\\/])")   # from a run's start: linear on long runs
# Characters a Windows account name can't hold, and brackets: a diagnostic's punctuation ends a name.
_NAME_END = set("/\"<>|*?:\r\n\t[];=,+()")
_NAME_MAX = 64
_SHARED_PROFILES = {"public", "default", "all users", "default user", "<user>"}
_HEX = set("0123456789abcdefABCDEF")


def _profile_name_end(text: str, i: int, sep: str) -> tuple[int, bool]:
    r"""Where the folder name starting at text[i] ends, and whether a separator ends it (so it's
    certainly a whole folder, spaces and all): a / always, a backslash run as wide as `sep`. A \uXXXX
    escape or an apostrophe inside a word is part of it; a narrower backslash run before anything else
    (an escaped quote) ends it, as does running past any account name's length in characters."""
    j, n, chars = i, len(text), 0
    while j < n and chars < _NAME_MAX:
        chars += 1
        c = text[j]
        if c == "\\":
            k = j
            while k < len(text) and text[k] == "\\":
                k += 1
            if sep[0] == "\\" and k - j >= len(sep):
                return j, _one_folder(text[i:j])
            if text[k:k + 1] == "u" and k + 5 <= len(text) and all(ch in _HEX for ch in text[k + 1:k + 5]):
                j = k + 5
                continue
            return j, False
        if c == "/":
            return j, _one_folder(text[i:j])
        if c in _NAME_END:
            return j, False
        if c == "'" and not (j + 1 < len(text) and (text[j + 1].isalnum() or text[j + 1] == "\\")):
            return j, False
        j += 1
    return j, False


def _one_folder(name: str) -> bool:
    """Whether what lies between two separators reads as one folder name rather than a message that runs
    on to a later path (.\\backup): a display name has a few words at most, and none is a lone dot."""
    words = name.split()
    return len(words) <= 3 and not any(w.strip(".") == "" for w in words)


def _scrub_profiles(text: str, known: re.Pattern | None = None) -> str:
    r"""The folder after a Users directory (C:\Users\name, /Users/name), at any depth of JSON escaping:
    the run of separators before "Users" says how wide a separator is in this text. A whole folder goes;
    failing that, a known account's name (`known`, matched in place) wherever the path ends; failing
    that, its first word."""
    out, last = [], 0
    for m in _PROFILES_DIR.finditer(text):
        if m.start() < last:
            continue
        sep = m[1]
        start = m.end() + len(sep)
        if text[m.end():start] != sep:
            continue
        end, whole = _profile_name_end(text, start, sep)
        if not whole:
            hit = known.match(text, start) if known else None
            if hit:
                end = hit.end()
            else:   # nothing after it says where it ends: a name with no spaces, as far as we can tell
                end = next((k for k in range(start, end) if text[k].isspace()), end)
        name = text[start:end]
        if not name or name.lower() in _SHARED_PROFILES:
            continue
        out += [text[last:start], "<user>"]
        last = end
    return "".join(out) + text[last:]


def _account_names() -> set[str]:
    return {n for n in (os.environ.get("USERNAME"), os.environ.get("USER"), Path.home().name) if n}


def _escaped_forms(name: str, depth: int = 4) -> set[str]:
    r"""The name as it is, and as each further level of JSON escaping writes it (\uXXXX past ASCII)."""
    forms = set()
    for form in {name, name.lower(), name.upper(), name.title(), name.capitalize()}:   # \u00e9 isn't \u00c9
        forms.add(form)
        for _ in range(depth):
            form = json.dumps(form)[1:-1]
            forms.add(form)
    return forms


class Scrubber:
    r"""Blanks the .env's values out of text: every secret-looking key's value, and anything else long
    enough not to be an ordinary word or number. A short one only where it stands alone, so a two-letter
    topic doesn't blank every word with those letters in it. Then account names: any profile folder
    under Users, whole, and this PC's own account wherever it's a folder in a path (a profile moved to
    D:\Profiles\name, a vendor's folder per player, a relative path in a listing)."""

    def __init__(self, pairs, accounts: set[str] | None = None) -> None:
        values = sorted({v for k, v in pairs if v and (len(v) >= 6 or SECRET_KEY.search(k))}, key=len, reverse=True)
        self.patterns = [re.compile(re.escape(v) if len(v) >= 6 else rf"(?<![\w-]){re.escape(v)}(?![\w-])")
                         for v in values]
        names = sorted({f for n in (_account_names() if accounts is None else accounts) for f in _escaped_forms(n)},
                       key=len, reverse=True)
        self.lowered = {n.lower() for n in names}
        self.known = re.compile("(?i)(?:" + "|".join(map(re.escape, names)) + r")(?!\w)") if names else None
        # After a separator and before a separator or the string's end; or, starting a relative path,
        # after a space or quote and before a separator (so a player's name in a sentence stays).
        alts = "|".join(map(re.escape, names))
        self.accounts = re.compile(rf"(?i)(?:(?<=[\\/])(?:{alts})(?=[\\/\"'\s:;,)\]}}]|$)"
                                   rf"|(?<![^\s\"'(\[])(?:{alts})(?=[\\/]))") if names else None

    def __call__(self, text: str) -> str:
        for p in self.patterns:
            text = p.sub("<redacted>", text)
        text = _scrub_profiles(text, self.known)
        if self.accounts and ("\\" in text or "/" in text):   # one pass, and only where there's a path
            text = self.accounts.sub(lambda m: self._account_match(text, m), text)
        return text

    def _account_match(self, text: str, m: re.Match) -> str:
        r"""An escaped form starting with backslashes (\u00c9lodie) only where the run before it is a
        separator twice as wide, so it never takes one of the separator's backslashes and leaves broken
        JSON. The one alternation takes the longest spelling, so a run too long for it is re-split: a
        third of the whole run is the name's own escape."""
        lead = len(m[0]) - len(m[0].lstrip("\\"))
        if lead:
            k = m.start()
            while k > 0 and text[k - 1] == "\\":
                k -= 1
            run = m.start() - k
            if run and run != 2 * lead:
                own = (run + lead) // 3
                if (run + lead) % 3 or own >= lead or m[0][lead - own:].lower() not in self.lowered:
                    return m[0]
                return m[0][:lead - own] + "<user>"
        return "<user>"


def _decode(data: bytes) -> str:
    """UTF-8, or UTF-16 for a Windows program's log (its NULs would keep a path from matching a scrub),
    even tailed from an odd byte."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data:   # the whole of it: a localized log's first pages may hold no ASCII at all
        odd, even = data[1::2].count(0), data[0::2].count(0)
        if odd >= 4 and even * 8 < odd:
            return data[: len(data) // 2 * 2].decode("utf-16-le", errors="replace")
        if even >= 4 and odd * 8 < even:
            return data[1:][: (len(data) - 1) // 2 * 2].decode("utf-16-le", errors="replace")
    text = data.decode("utf-8", errors="replace")
    return text.replace("\x00", "") if "\x00" in text else text   # unsure: no NUL may split a path from its scrub


# Other programs' files (GSPro's, a launch monitor's) can hold an account's sign-in: these go
# whatever the .env says. A credential-named key or tag keeps its name and loses its value. Each
# pattern starts only where a run begins (the lookbehinds), so a long run of letters is scanned once,
# and a credential's value is walked once by hand: a string at any depth of escaping, or [...] / {...}.
_AFTER_ESCAPE = r"(?<=\\[nrt])"   # a JSON string's \n starts a word too, and its n isn't part of it
_SECRET_NAME = r"[\w-]*(?:password|passwd|pwd|token|secret|api[_-]?key|apikey|subscription[_-]?key|speech[_-]?key|access[_-]?key(?:[_-]?id)?|authorization|cookie|license[_-]?key)\b"
_EMAIL = re.compile(rf"(?:(?<![\w.+\\-])|{_AFTER_ESCAPE})[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_JWT = re.compile(rf"(?:(?<![\w\\-])|{_AFTER_ESCAPE})eyJ[\w-]{{8,}}\.[\w-]{{8,}}(?:\.[\w-]+)?")
_BEARER = re.compile(rf"(?i)(?:(?<![\w\\-])|{_AFTER_ESCAPE})(bearer|basic)\s+[\w.~+/=-]{{8,}}")
# A cookie or authorization header runs to the line's end, or to the end of the JSON string it's logged in
# (a quote then , } or ] — but not `", nonce="`, a Digest header's next parameter).
_COOKIE_HEADER = re.compile(rf"(?im)((?:(?<![\w\\-])|{_AFTER_ESCAPE})(?:(?:set-)?cookie|(?:proxy-)?authorization)[ \t]*:[ \t]*)[^\r\n]*?(?=(?<!\\)\\*+\"\s*(?:[}}\]]|,(?!\s*[\w-]+=))|(?<!\\)\\+[rn]|\r?$)")
_XML_SECRET = re.compile(rf"(?i)<((?:[\w-]+:)?{_SECRET_NAME})(\s[^<>]*(?<!/))?>"     # the text, or a CDATA's, never
                         r"(?:(\s*+<!\[CDATA\[)(?:[^\]]|\](?!\]>))*+|[^<]*+)")              # failing, so each runs once
_CREDENTIAL_KEY = re.compile(rf"(?i)(?:(?<![\w\\-])|{_AFTER_ESCAPE})(\\*[\"'])?{_SECRET_NAME}(\\*[\"'])?[ \t]*[:=][ \t]*")
VALUE_CAP = 65536   # an [...] or {...} longer than this was never closed
# A pasted line setting a credential, typed by hand: all the rest of the line is the value.
_SECRET_LINE = re.compile(rf"(?im)^([ \t]*(?:export[ \t]+)?{_SECRET_NAME}[ \t]*[:=][ \t]*)\S.*$")
_CLOSES = re.compile(r"\s*(?:[,}\]:]|$)", re.M)
_SPACE = re.compile(r"\s*")
_QUOTE = re.compile(r"\\*[\"']")
_WORD = re.compile(r"(?:[^\s,;}\]&<\"\\]++|\\++(?![nrt\"]))*+")   # an escape (at any depth) of \n \r \t \" ends it
_LONG_BLOB = re.compile(rf"(?:(?<![\w+=\\-])|{_AFTER_ESCAPE})[A-Za-z0-9+_-]{{40,}}={{0,2}}(?![\w+=-])")   # a key or token, not a word


def _string_end(text: str, i: int, quote: str) -> int:
    """Where the string opened by `quote` (a quote after its escapes, at whatever depth of JSON in
    JSON) at `i` ends; a quote behind more backslashes is inside it. A line's end stops it."""
    slashes, q = len(quote) - 1, quote[-1]
    j = i + len(quote)
    while j < len(text):
        c = text[j]
        if c == "\n":
            return j
        if c == q:
            k = j
            while k > i + len(quote) and text[k - 1] == "\\":
                k -= 1
            n = j - k   # an escaped backslash inside adds 2 * (slashes + 1)
            if n >= slashes and (n - slashes) % (2 * (slashes + 1)) == 0:
                return j + 1
        j += 1
    return j


def _value_end(text: str, i: int, quote: str) -> int:
    """The end of the value at `i`: a string, an array or object (to its closing bracket), or a word."""
    if quote and text.startswith(quote, i):
        return _string_end(text, i, quote)
    if i < len(text) and text[i] in "[{":
        depth, j = 0, i
        while j < len(text) and j - i < VALUE_CAP:
            if quote and text.startswith(quote, j):
                j = _string_end(text, j, quote)
                continue
            depth += {"[": 1, "{": 1, "]": -1, "}": -1}.get(text[j], 0)
            j += 1
            if depth == 0:
                return j
        return text.find("\n", i) % (len(text) + 1)   # unclosed: the rest of the line
    return _WORD.match(text, i).end()


def _blank_credentials(text: str) -> str:
    """A credential-named key keeps its name and loses its value, in plain text, key=value, or JSON
    nested in JSON strings to any depth (a quoted key's value becomes a string, so JSON stays JSON)."""
    out, at = [], 0
    for m in _CREDENTIAL_KEY.finditer(text):
        if m.start() < at:
            continue
        keyed = m.group(1) if m.group(1) == m.group(2) else ""   # "key": not "message": "key=
        start = _SPACE.match(text, m.end()).end() if keyed else m.end()   # only JSON's value goes to the next line
        opened = _QUOTE.match(text, start)
        if opened and not keyed and _CLOSES.match(text, opened.end()):
            continue   # "message": "... password=" — that quote closes the message
        quote = opened[0] if opened else keyed or ""
        end = _value_end(text, start, quote)
        value = text[start:end]
        if not value or value.lower() in ("null", "true", "false"):
            continue
        out.append(text[at:start])
        out.append(quote + "<redacted>" + quote)
        at = end
    out.append(text[at:])
    return "".join(out)


def scrub_private(text: str, blobs: bool = True) -> str:
    """`blobs`: also any long run of letters and digits that could be a bare key or a device's id (not
    for GSPro's own log, where they're its objects' and courses' names)."""
    text = _JWT.sub("<redacted>", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} <redacted>", text)
    text = _COOKIE_HEADER.sub(lambda m: m.group(1) + "<redacted>", text)
    text = _XML_SECRET.sub(lambda m: f"<{m.group(1)}{m.group(2) or ''}>{m.group(3) or ''}<redacted>", text)
    text = _blank_credentials(text)
    text = _EMAIL.sub("<email>", text)
    if not blobs:
        return text
    return _LONG_BLOB.sub(lambda m: "<redacted>" if re.search(r"\d", m[0]) and re.search(r"[A-Za-z]", m[0]) else m[0],
                          text)


def scrub_jsonl(data: bytes, scrub) -> str:
    """`scrub` applied to each value of each JSON line rather than the line's text, so a redacted number
    becomes the string "<redacted>" and every line still parses. A line that doesn't parse is dropped."""
    def walk(v):
        if isinstance(v, dict):
            return {scrub(k): walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            return scrub(v)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and scrub(str(v)) != str(v):
            return "<redacted>"
        return v

    out = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            out.append(json.dumps(walk(json.loads(line))))
        except ValueError:   # a write cut off mid-record: its secrets may be spelled escaped, so none of it
            out.append(json.dumps({"note": "unreadable line left out"}))
    return "".join(f"{line}\n" for line in out)


def env_files(config_path: Path, paired: bool = False) -> list[Path]:
    """Both .env files the booth loads (__main__._load_env): beside the code and beside config.yaml
    (`paired`, a Shotcast Cloud profile's: its own only)."""
    if paired:
        return [config_path.resolve().parent / ".env"]
    from . import dotenv

    return list(dict.fromkeys((dotenv.CODE_ENV, config_path.resolve().parent / ".env")))


def tail(path: Path, cap: int) -> bytes:
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > cap:
            f.seek(size - cap)
            f.readline()   # start on a whole line
            note = b'{"note":"earlier lines left out"}\n' if path.suffix.lower() == ".jsonl" else \
                b"[... earlier lines left out ...]\n"   # a JSONL file stays one JSON record a line
            return note + f.read()
        return f.read()


def _describe(path: Path) -> str:
    try:
        st = path.stat()
    except FileNotFoundError:
        return f"{path}: missing"
    except OSError as e:
        return f"{path}: can't look ({e.strerror or e!r})"
    return f"{path}: {st.st_size:,} bytes, modified {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}"


# The PC, as PowerShell's CIM sees it, in one JSON object. A video card's AdapterRAM stops at 4 GB, so
# its memory comes from the driver's registry key. Processes: GSPro's and the booth's (python).
_PC_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$cls = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*'
$vram = @{}
Get-ItemProperty $cls | ForEach-Object { if ($_.DriverDesc) { $vram[$_.DriverDesc] = $_.'HardwareInformation.qwMemorySize' } }
[pscustomobject]@{
  cpu = @(Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed, LoadPercentage)
  os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption, TotalVisibleMemorySize, FreePhysicalMemory
  gpu = @(Get-CimInstance Win32_VideoController | ForEach-Object { [pscustomobject]@{ Name = $_.Name;
        DriverVersion = $_.DriverVersion; Memory = $vram[$_.Name]; Mode = $_.VideoModeDescription;
        RefreshRate = $_.CurrentRefreshRate } })
  power = ((powercfg /getactivescheme) -replace '.*\((.*)\).*', '$1')
  battery = @(Get-CimInstance Win32_Battery | Select-Object BatteryStatus, EstimatedChargeRemaining)
  procs = @(Get-Process GSPro, python, pythonw | Select-Object Name, Id, CPU, WorkingSet64)
} | ConvertTo-Json -Depth 4 -Compress
"""


def _gb(n) -> str:
    return f"{n / 2**30:.1f} GB" if isinstance(n, (int, float)) and n > 0 else "?"


def pc_lines(info: dict) -> list[str]:
    """The PC's processor, memory, video cards, power and the processes that matter, from _PC_SCRIPT's
    JSON: why one booth reads the screen slower than another."""
    as_list = lambda v: v if isinstance(v, list) else [v] if v else []   # noqa: E731 (one item: no list)
    lines = []
    for c in as_list(info.get("cpu")):
        lines.append(f"CPU: {(c.get('Name') or '?').strip()}, {c.get('NumberOfCores')} cores /"
                     f" {c.get('NumberOfLogicalProcessors')} threads, {c.get('MaxClockSpeed')} MHz,"
                     f" {c.get('LoadPercentage')}% busy now")
    os_ = info.get("os") or {}
    if os_:
        total, free = os_.get("TotalVisibleMemorySize"), os_.get("FreePhysicalMemory")
        lines.append(f"Memory: {_gb(total * 1024 if total else None)} total, {_gb(free * 1024 if free else None)} free"
                     f" ({os_.get('Caption') or 'Windows'})")
    for g in as_list(info.get("gpu")):
        mode = f", {g['Mode']} at {g.get('RefreshRate')} Hz" if g.get("Mode") else ""   # blank: no display on it
        lines.append(f"Video card: {g.get('Name')}, {_gb(g.get('Memory'))}, driver {g.get('DriverVersion')}{mode}")
    if info.get("power"):
        lines.append(f"Power plan: {str(info['power']).strip()}")
    for b in as_list(info.get("battery")):   # BatteryStatus 1 is on battery, 2 on mains
        lines.append(f"Battery: {b.get('EstimatedChargeRemaining')}%,"
                     f" {'ON BATTERY' if b.get('BatteryStatus') == 1 else 'plugged in'}")
    for p in as_list(info.get("procs")):
        lines.append(f"Process {p.get('Name')} ({p.get('Id')}): {p.get('CPU') or 0:.0f} s of CPU,"
                     f" {_gb(p.get('WorkingSet64'))} in memory")
    return lines


def pc_info() -> list[str]:
    """pc_lines for this PC (Windows); elsewhere what Python knows."""
    if os.name != "nt":
        return [f"CPU: {platform.processor() or platform.machine()}, {os.cpu_count()} threads"]
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PC_SCRIPT],
                         capture_output=True, text=True, timeout=60,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return pc_lines(json.loads(out))


def screen_layout(cfg: Config) -> list[str]:
    """The monitors, the one the booth reads, and where GSPro's window is."""
    from .screen import monitor_of

    lines = [f"screen.capture: {cfg.screen.capture}   screen.follow_window: {cfg.screen.follow_window}"
             f"   screen.monitor: {cfg.screen.monitor}   screen.window_title: {cfg.screen.window_title!r}"
             f"   screen.ocr: {cfg.screen.ocr}"]
    window = None
    try:
        from . import winshot

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
    on = monitor_of(window["rect"], monitors) if window else None
    reads = on if cfg.screen.follow_window and on is not None else cfg.screen.monitor
    for i, m in enumerate(monitors):
        if i:
            tag = " <- the booth reads this one" if i == reads else ""
            lines.append(f"Monitor {i}: {m['width']}x{m['height']} at ({m['left']}, {m['top']}){tag}")
    if monitors and reads >= len(monitors):
        lines.append(f"!! screen.monitor is {reads} but there are only {len(monitors) - 1} monitors")
    if window is None:
        lines.append(f"GSPro window: none found with {cfg.screen.window_title!r} in its title (not running?)")
    else:
        left, top, right, bottom = window["rect"]
        lines.append(f"GSPro window: {window['title']!r} {right - left}x{bottom - top} at ({left}, {top})"
                     f"{' MINIMIZED' if window['minimized'] else ''}, on monitor {on}")
        if (c := window.get("client")) and on is not None and on < len(monitors):
            m = monitors[on]
            full = c == (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])
            lines.append(f"GSPro view: {c[2] - c[0]}x{c[3] - c[1]} at ({c[0]}, {c[1]})"
                         f"{'' if full else ' (windowed)'}")
        if on is not None and on != reads:
            lines.append(f"!! GSPro is on monitor {on} but the booth reads monitor {reads}")
    return lines


def _db_summary(db_path: Path, days: int = 14) -> tuple[list[str], list[tuple[str, bytes]]]:
    """Recent sessions' shot and map counts, and the hole maps kept lately."""
    if not db_path.exists():
        return [f"No season database at {db_path}"], []
    lines, images = [], []
    # not mode=ro: that can't read a WAL file no other connection has open (stats_web.connect)
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=rw", uri=True)
    con.execute("PRAGMA query_only = ON")
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


# Files a launch monitor's own software leaves on the PC, for learning a monitor's data from a
# report by someone who has one: (what, folder, glob, newest how many, bytes kept from each).
LM_FILES: list[tuple[str, str, str, int, int]] = [
    ("Uneekor VIEW shots", r"%USERPROFILE%\AppData\LocalLow\UNEEKOR\VIEW\ShotData", "*/*.json", 12, 200_000),
    ("Uneekor VIEW log", r"%USERPROFILE%\AppData\LocalLow\UNEEKOR\VIEW", "Player*.log", 2, 1_000_000),
    ("Foresight SDK logs", r"%PROGRAMDATA%\Foresight", "FSS_SDK_*LOG.txt", 2, 1_000_000),
    ("Foresight FSX last shot", r"%ProgramFiles(x86)%\Foresight Sports Experience\System", "LastShot.CSV", 1, 200_000),
    ("rela log", r"%LOCALAPPDATA%\rela", "rela*.log", 2, 2_000_000),
    ("rela log (temp)", r"%TEMP%", "rela*.log", 1, 2_000_000),
]


def _skytrak_files() -> list[tuple[str, str, str, int, int]]:
    """LM_FILES entries for the SkyTrak app's BepInEx log (OpenSkyPlus 2) and OpenSkyPlus 1's own log,
    wherever the app is installed."""
    from .sources.club_feeds import skytrak_dirs

    out = []
    for d in skytrak_dirs():
        if (d / "BepInEx").is_dir():
            out += [("SkyTrak BepInEx log", str(d / "BepInEx"), "LogOutput*.log", 2, 2_000_000),
                    ("OpenSkyPlus 1 log", str(d / "BepInEx" / "plugins" / "OpenSkyPlus"), "Log.txt", 1, 2_000_000)]
    return out


# springbok's MLM2PRO-GSPro-Connector logs a Rapsodo account's token and web API secret in among its
# shots, so only these of its lines go in a report.
SPRINGBOK_KEEP = ("App Version:", "Sending to GSPro data:", "GSProConnect: Success:", "Received shot data from device:",
                  "Ball data:", "Launch Ball payload:")


def springbok_shot_lines(data: bytes) -> bytes:
    return b"".join(line for line in data.splitlines(keepends=True)
                    if any(k.encode() in line for k in SPRINGBOK_KEEP))


CONNECTOR_LOGS = 4          # rotated ConnectDebug files kept, newest first
GSPRO_DB_ROWS = 20          # newest rows of each GSPro.db shot table
# Where launch monitor software installs, and the vendor names its folders go by: the report lists
# what's in them (names, sizes, dates; no contents), to show where a monitor we haven't seen keeps
# its shots.
SOFTWARE_ROOTS = [r"%PROGRAMDATA%", r"%ProgramFiles%", r"%ProgramFiles(x86)%", r"%APPDATA%", r"%LOCALAPPDATA%",
                  r"%USERPROFILE%\AppData\LocalLow", r"%USERPROFILE%\Documents"]
LM_VENDORS = re.compile(r"aboutgolf|^ag[ _-]|uneekor|foresight|bushnell|flightscope|mevo|garmin|rapsodo|mlm2|"
                        r"square ?golf|full ?swing|protee|skytrak|trugolf|trackman|nova|golfjoy|vtrack|bluetees|"
                        r"rainmaker|voice ?caddie|swing ?caddie|golfzon|gsp.*connect|launch ?monitor", re.I)
LISTING_DEPTH = 3
LISTING_PER_FOLDER = 40
LISTING_LINES = 3000
LISTING_SKIP = {"cache", "ebwebview", "crashpad", "gpucache", "code cache"}   # an embedded browser's own files


def _software_listing() -> list[str]:
    """Every launch monitor vendor's folder under SOFTWARE_ROOTS, LISTING_DEPTH levels down."""
    out: list[str] = []
    for root in dict.fromkeys(Path(os.path.expandvars(r)) for r in SOFTWARE_ROOTS):
        try:
            tops = sorted(p for p in root.iterdir() if p.is_dir() and LM_VENDORS.search(p.name))
        except OSError:
            continue
        for top in tops:
            out.append(f"{top}/")
            stack = [(top, 1)]
            while stack and len(out) < LISTING_LINES:
                d, depth = stack.pop()
                try:
                    kids = sorted(d.iterdir(), key=_mtime, reverse=True)
                except OSError as e:
                    out.append(f"  {d}: can't list ({e.strerror or e!r})")
                    continue
                if len(kids) > LISTING_PER_FOLDER:
                    out.append(f"  {d.relative_to(top).as_posix()}: {len(kids)} entries, the newest {LISTING_PER_FOLDER} listed")
                    kids = kids[:LISTING_PER_FOLDER]
                for k in kids:
                    rel = k.relative_to(top).as_posix()
                    try:
                        st = k.stat()
                    except OSError:
                        continue
                    if k.is_dir():
                        out.append(f"  {rel}/")
                        if depth < LISTING_DEPTH and k.name.lower() not in LISTING_SKIP:
                            stack.append((k, depth + 1))
                    else:
                        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                        out.append(f"  {rel}  {st.st_size:,}  {stamp}")
            if len(out) >= LISTING_LINES:
                return out + ["[... listing cut short ...]"]
    return out


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _newest(folder: Path, pattern: str, n: int) -> list[Path]:
    return sorted((p for p in folder.glob(pattern) if p.is_file()), key=_mtime, reverse=True)[:n]


def _gspro_db_shots(db: Path) -> tuple[list[str], dict]:
    """GSPro.db's tables, and the newest rows of the ones holding shots (any launch monitor's ball and
    club data, as GSPro took it). Read-only, giving up quickly if GSPro holds the lock."""
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
    try:
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
        rows = {}
        for t in ("LastShot", "DrivingRangeShot"):
            if t in tables:
                rows[t] = [{"ID": i, "DateCreated": d, "ShotData": json.loads(s) if s else None}
                           for i, d, s in con.execute(f"select ID, DateCreated, ShotData from {t} "
                                                      f"order by ID desc limit {GSPRO_DB_ROWS}")]
    finally:
        con.close()
    return tables, rows


def _last_lm_type(logs: list[Path]) -> str | None:
    """The launch monitor GSPro last logged a shot from (its `LM Type` line), from the newest log."""
    from .sources.gspro_log import _LM_TYPE

    for p in sorted((p for p in logs if p.is_file()), key=_mtime, reverse=True):
        for line in reversed(tail(p, GSPRO_LOG_CAP).decode("utf-8", errors="replace").splitlines()):
            if m := _LM_TYPE.match(line.strip()):
                return m.group(1)
    return None


def build(cfg: Config, config_path: Path = Path("config.yaml"), description: str = "",
          paired: bool = False) -> Path:
    """Write the report zip into folder(cfg) and return its path. `description` is the sender's own
    words on what went wrong (optional), at the top of summary.txt. `paired`: Shotcast started paired
    with Shotcast Cloud (cloud.startup), so it's the profile's report."""
    with _building:
        return _build(cfg, config_path, description, paired)


def _build(cfg: Config, config_path: Path, description: str = "", paired: bool = False) -> Path:
    from .updater import current_version

    log_dir = Path(cfg.log_dir)
    # Paired with Shotcast Cloud: the report is the profile's, so what GSPro and the launch monitor's
    # software kept (their logs, GSPro.db, the round file, and the booth's copies of GSPro's log and
    # round files), from before the pairing too, is left out.
    env: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    for p in env_files(config_path, paired):
        found = env_pairs(p)
        env.update(found)
        pairs += found   # every value, even one a later line or the other file overrides
    scrub = Scrubber(pairs)
    out_dir = folder(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    # A random tail: the CLI and the tray can make one in the same second, in separate processes.
    path = out_dir / f"shotcast-report-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.zip"
    summary: list[str] = [
        f"Shotcast problem report, {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Shotcast {current_version()} on {platform.platform()}, Python {platform.python_version()}",
        "",
        "== What went wrong (the sender's words) ==",
        scrub_private(_SECRET_LINE.sub(r"\1<redacted>", scrub(description.strip()))) or "(none given)",
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
        def entry(name: str) -> str:   # a member name is a folder to unzip into: no <> in it on Windows
            return scrub(name).replace("<user>", "_user_")

        def add_text(name: str, data: bytes, private: bool = False, blobs: bool = True) -> None:
            text = scrub(_decode(data))
            z.writestr(entry(name), scrub_private(text, blobs) if private else text)

        def add_file(name: str, p: Path, cap: int | None = None, private: bool = False, blobs: bool = True) -> None:
            if p.is_file():
                if p.suffix.lower() == ".png":   # a 4K PNG is ~12 MB: too big to send round
                    from PIL import Image

                    buf = io.BytesIO()
                    with Image.open(p) as img:
                        img.convert("RGB").save(buf, format="JPEG", quality=85)
                    z.writestr(entry(name[:-4] + ".jpg"), buf.getvalue())
                elif p.suffix.lower() in (".jpg", ".jpeg"):
                    z.write(p, entry(name))
                else:
                    add_text(name, tail(p, cap) if cap else p.read_bytes(), private, blobs)

        guard("screen layout", lambda: summary.extend(screen_layout(cfg)))

        summary += ["", "== PC =="]
        guard("PC", lambda: summary.extend(pc_info()))

        summary += ["", "== Performance =="]
        guard("perf log", lambda: summary.extend(perf_log.summarize(log_dir)))

        summary += ["", "== GSPro =="]
        source = cfg.source
        summary.append(f"source.type: {source.type}")
        if paired:
            summary.append("GSPro's and the launch monitor's own files: left out (paired with Shotcast Cloud)")
        summary.append("Connector log " + _describe(Path(source.connector_log)))
        for p in [] if paired else _gspro_logs(cfg):
            summary.append("GSPro log " + _describe(p))
            guard(f"GSPro log {p.name}", lambda p=p: add_file(f"gspro/{p.name}", p, GSPRO_LOG_CAP,
                                                                                   private=True, blobs=False))
        if not paired:
            guard("connector log", lambda: add_file("gspro/ConnectDebug.txt", Path(source.connector_log), 1_000_000,
                                                          private=True))

        def gspro_settings() -> None:
            from .gspro_settings import read_game_settings

            p = Path(source.gspro_log).expanduser().parent / "Settings.vgs"
            if not p.is_file():
                summary.append("GSPro settings: no Settings.vgs")
                return
            found = read_game_settings(p)
            summary.append("GSPro settings: " + ", ".join(f"{k}={v}" for k, v in found.items()))
        guard("GSPro settings", gspro_settings)

        summary += ["", "== Launch monitor =="]
        if not paired:
            guard("launch monitor type", lambda: summary.append(
                f"GSPro last logged: LM Type {_last_lm_type(_gspro_logs(cfg)) or '(none found)'}"))

        def connector_logs() -> None:
            live = Path(source.connector_log)
            for p in _newest(live.parent, live.name + ".*", CONNECTOR_LOGS):
                summary.append("Connector log " + _describe(p))
                add_file(f"gspro/{p.name}", p, 1_000_000, private=True)
        if not paired:
            guard("rotated connector logs", connector_logs)

        gspro_dir = Path(source.gspro_log).expanduser().parent

        def gspro_db() -> None:
            db = gspro_dir / "GSPro.db"
            if not db.is_file():
                summary.append(f"{db}: missing")
                return
            tables, rows = _gspro_db_shots(db)
            summary.append(f"GSPro.db tables: {', '.join(tables)}")
            summary.extend(f"GSPro.db {t}: {len(r)} newest rows kept" for t, r in rows.items())
            add_text("gspro/GSPro.db-shots.json", json.dumps(rows, indent=1, ensure_ascii=False).encode(), private=True,
                     blobs=False)
        if not paired:
            guard("GSPro.db shots", gspro_db)
            guard("round file", lambda: add_file("gspro/currentRound.dat", gspro_dir / "currentRound.dat", 2_000_000,
                                                           private=True, blobs=False))

        def round_files() -> None:
            from .round_file import ARCHIVE_DIR, newest_archives

            kept = newest_archives(log_dir)[:ROUND_FILES]
            summary.append(f"Round files kept from round ends: {len(kept)} (the newest {ROUND_FILES} at most)")
            for p in kept:
                # One JSON array: cut to its tail it's no use, so one too big is left out whole.
                if (size := p.stat().st_size) > ROUND_FILE_CAP:
                    summary.append(f"  {p.name}: {size / 1e6:.1f} MB, too big to include")
                else:
                    add_file(f"gspro/{ARCHIVE_DIR}/{p.name}", p, private=True, blobs=False)
        if not paired:
            guard("kept round files", round_files)

        def vendor_files() -> None:
            for what, where, pattern, n, cap in LM_FILES + _skytrak_files():
                base = Path(os.path.expandvars(where))
                found = _newest(base, pattern, n) if base.is_dir() else []
                summary.append(f"{what}: {len(found)} file(s) from {base}" if found else f"{what}: none")
                tag = re.sub(r"[^\w.-]+", "_", what)
                for p in found:
                    add_file(f"launch-monitor/{tag}/{p.relative_to(base).as_posix()}", p, cap, private=True)
            from .sources.club_feeds import SPRINGBOK_LOGS, springbok_dirs

            for i, base in enumerate(springbok_dirs()):   # a running connector's, as it has no fixed folder
                found = _newest(base, SPRINGBOK_LOGS, 2)
                summary.append(f"springbok connector log: {len(found)} file(s) from {base} (shot lines only)")
                for p in found:
                    add_text(f"launch-monitor/springbok_connector_log/{i}/{p.name}",
                             springbok_shot_lines(tail(p, 2_000_000)), private=True)
        if not paired:
            guard("launch monitor files", vendor_files)

        def software() -> None:
            lines = _software_listing()
            summary.append(f"Launch monitor software folders: {sum(1 for l in lines if not l.startswith(' '))}"
                           " (listed in launch-monitor/software.txt)")
            add_text("launch-monitor/software.txt", "\n".join(lines).encode(), private=True, blobs=False)
        guard("launch monitor software", software)

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
            for pattern in ("booth-*.log", "tray-*.log") + (() if paired else ("gspro-*.log",)):
                for p in sorted(log_dir.glob(pattern)):
                    if p.name != "booth-errors.log" and p.stat().st_mtime >= cutoff:
                        add_file(f"logs/{p.name}", p, LOG_CAP)
            add_file("logs/booth-errors.log", log_dir / "booth-errors.log", 1_000_000)
            add_file("logs/live.json", log_dir / "live.json")
        guard("logs", logs)

        def prompt_logs() -> None:
            # All still on disk (the booth prunes them to a week), each on its own: one gone or locked
            # mustn't lose the newer days.
            for p in sorted(log_dir.glob(f"{prompt_log.PREFIX}*.jsonl")):
                guard(f"prompt log {p.name}", lambda p=p: z.writestr(
                    f"logs/{p.name}", scrub_jsonl(prompt_log.excerpt(p, PROMPT_LOG_CAP), scrub)))
        guard("prompt logs", prompt_logs)
        for p in sorted(log_dir.glob(f"{perf_log.PREFIX}*.jsonl")):   # ~200 KB a day of play
            guard(f"perf log {p.name}", lambda p=p: z.writestr(
                f"logs/{p.name}", scrub_jsonl(tail(p, PERF_LOG_CAP), scrub)))

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


def keep_frame(log_dir: str | Path, reason: str, image, meta: dict, keep: int = 4,
               crops: dict | None = None) -> Path:
    """Save a screen the booth found something wrong with, for the next problem report: a full-size
    JPEG and a JSON note beside it, keeping the newest `keep` of each reason. `crops`: the HUD boxes
    the read was actually made from (the screen may have moved on by the time it's grabbed), each a
    PNG beside it ({stem}.{box}.png)."""
    d = Path(log_dir) / FRAMES_DIR
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{reason}-{time.strftime('%Y%m%d-%H%M%S')}"
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=85)
    (d / f"{stem}.jpg").write_bytes(buf.getvalue())
    (d / f"{stem}.json").write_text(json.dumps({"captured": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason,
                                                "size": list(image.size), **meta}, default=str), encoding="utf-8")
    for name, crop in (crops or {}).items():
        if hasattr(crop, "save"):   # (not the other side's probe dict)
            crop.save(d / f"{stem}.{name}.png")
    for old in sorted(d.glob(f"{reason}-*.jpg"))[:-keep]:
        try:
            for p in d.glob(f"{old.stem}.*"):
                p.unlink(missing_ok=True)
        except OSError:
            log.warning("Couldn't remove the old screen %s", old.name)
    return d / f"{stem}.jpg"


CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # the report drop's (tools/aws/report-drop.yaml)


def show_code(code: str) -> str:
    """ABCD-EFGH: easier to read out or type than eight in a row."""
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def parse_code(text: str) -> str:
    """A report code as typed (any case, with dashes or spaces), or ValueError."""
    code = re.sub(r"[\s-]", "", text).upper()
    if len(code) != 8 or any(c not in CODE_ALPHABET for c in code):
        raise ValueError(f"{text!r} isn't a report code (eight letters and digits, like ABCD-EFGH)")
    return code


def upload(path: Path, url: str, timeout: float = 120.0) -> str:
    """Send a report zip to a report drop and return its code. The drop hands out a one-time,
    size-capped upload link; the zip goes straight to its bucket."""
    import httpx

    size = path.stat().st_size
    r = httpx.post(url, json={"size": size}, timeout=30.0)
    if r.status_code == 429:
        raise RuntimeError(r.json().get("error") or "The report drop is busy; try again tomorrow.")
    r.raise_for_status()
    drop = r.json()
    if size > int(drop.get("max_bytes") or size):
        raise RuntimeError(f"The report is {size / 1e6:.0f} MB, over the drop's "
                           f"{int(drop['max_bytes']) / 1e6:.0f} MB. Send the zip some other way.")
    with path.open("rb") as f:
        # S3 wants the policy fields first and the file last; httpx sends data before files.
        up = httpx.post(drop["url"], data=drop["fields"], files={"file": (path.name, f, "application/zip")},
                        timeout=timeout)
    up.raise_for_status()
    code = parse_code(drop["code"])
    log.info("Problem report %s sent as %s", path.name, show_code(code))
    return code


def fetch(code: str, out_dir: Path, stack: str = "shotcast-report-drop", region: str | None = None,
          profile: str | None = None) -> Path:
    """Download a sent report by its code, with the report drop's AWS account (the maintainer's)."""
    import boto3

    code = parse_code(code)
    session = boto3.Session(profile_name=profile, region_name=region)
    outputs = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    bucket = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "BucketName")
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"shotcast-report-{code}.zip"
    try:
        session.client("s3").download_file(bucket, f"kept/{code}.zip", str(dest))
    except Exception as e:
        if getattr(e, "response", {}).get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            raise ValueError(f"No report under {show_code(code)}: mistyped, not a Shotcast report, "
                             "or over two weeks old") from None
        raise
    return dest
