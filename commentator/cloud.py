"""Shotcast Cloud from the sim PC: pair it as a bay of a tenant (a home league or a venue).

Hidden for now (there's no sign-up yet): nothing changes until this PC is paired, which is only done
from the command line, and a pairing (or unpairing) takes effect when Shotcast next starts:

    python -m commentator.cloud pair --api https://app.shotcastgolf.com [--name "Sim PC"]
    python -m commentator.cloud status
    python -m commentator.cloud upload      send settled rounds now
    python -m commentator.cloud unpair [--force]

Paired, the PC is a capture device (shotcast-cloud's docs/dev/thin-pc.md): it records rounds and sends
them up (cloud_rounds), with no league of its own (every round is practice here; the cloud decides
the rest). It runs as a separate Shotcast, a cloud profile: the pairing marker (`marker`, with the
tokens) is the machine's, at a fixed place that no config points to (`root`), and pairing makes the
profile's own Shotcast home beside it (`home_for`): its config.yaml, .env, season file and logs.
Everything (the booth, the tray, the command line) looks for the marker first, as it starts
(`locate`): with none, it's the self-hosted Shotcast, exactly as before; with one, it runs from the
profile's home and never opens the self-hosted config.yaml or .env, nor uses the service keys it was
started with (`clean_env`). A marker that's there but can't be trusted (unreadable, edited, made on
another PC, its profile or season file missing or another pairing's, its config.yaml invalid) is
damage (`Damaged`): nothing starts, and it never falls back to the self-hosted Shotcast; only `cloud
status` and `cloud unpair --force` run, to repair it. A pairing the API no longer accepts (a refused
refresh) still captures in its profile, sending nothing, and the tray says so. While Shotcast runs
paired it holds its profile's lifecycle lock (`running`), so it isn't unpaired from under it. The
profile's config.yaml was seeded at pair time from the self-hosted one's allow-list (`CARRIED`, less
private bits) and is read through `booth_config`, which applies the allow-list again and forces the
self-hosted booth's outbound jobs off, so a feature added later is off when paired unless it's added
to the list on purpose.

The PC never holds AWS credentials. It has a device refresh token (stored with Windows DPAPI, so only
this Windows user can read it, protected together with the API address, tenant and device it was
issued for, and the machine it was paired on) that it trades for 10-minute access tokens, rotating it each time under a lock shared by
every process (the booth's, the tray's, the command line's).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ROOT_ENV = "SHOTCAST_CLOUD_DIR"      # (tests) where the marker and profiles are, instead of the machine's
MARKER = "device.json"               # in root(): this PC's pairing, with its tokens
FOLDER_FILE = "pairing.json"         # in a profile's home: the folder_key it was made for
DEVICE_HEADER = "X-Shotcast-Device"
MIN_VERSION_HEADER = "X-Shotcast-Min-Version"
BODY_HASH = "x-amz-content-sha256"   # CloudFront's signed hop to the API needs every POST/PUT body's hash
REFRESH_EARLY_S = 60                 # refresh an access token this long before it runs out
REFRESH_WINDOW_S = 40                # retries of a refresh stay inside the API's 60 s grace for a spent token
REFRESH_TIMEOUT_S = 10.0
STORE_VERSION = 3                    # 3: the API, tenant, device and machine are protected with the tokens
_ENTROPY = b"shotcast-cloud-device"

# What a paired booth's config keeps from config.yaml (booth_config, and the profile's seed): capture
# (the GSPro connection and the screen read), commentary and voice (the personas, their asides'
# inbound feeds), the AI and voice services, the on-screen overlay and the booth's own health checks,
# and the PC's resource limits. Cross-checked with settings_owner's map, which doesn't fit as is: it
# says who edits a setting, not whether it's the self-hosted booth's (minimap and llm are "service",
# squabbit and live are "pc").
CARRIED = ("personas", "llm", "tts", "source", "screen", "commentary", "news", "weather", "scores", "trivia",
           "putt_read", "minimap", "overlay", "health", "process_priority", "max_cpu_cores", "perf_log")
# The .env keys a profile gets at pair time: the AI and voice services' (the booth still calls them
# from the PC). Nothing else (AWS, ntfy, Squabbit, SuperSaaS, OBS, TypeSafe) is copied.
ENV_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_TTS_API_KEY", "AZURE_SPEECH_KEY")
FIX = ("To repair it: `python -m commentator.cloud unpair --force`, then pair this PC again "
       "(`python -m commentator.cloud pair --api https://...`).")
REPAIR = ("Shotcast Cloud no longer accepts this PC: remove it from the sims on the site, then "
          "`python -m commentator.cloud unpair --force` and pair it again")
LIFECYCLE = ("run", "tray")          # <home>/<who>.lock: held by a paired booth (`run`) and tray (`running`)
# Inherited from the environment Shotcast was started in, and removed from it when paired (clean_env),
# so a paired booth only uses its profile's .env: the AI and voice services' keys and endpoint
# overrides, and the self-hosted booth's services' (AWS, ntfy, Squabbit, SuperSaaS, OBS, TypeSafe).
SCRUBBED = ("OPENAI_", "ANTHROPIC_", "CLAUDE_", "GOOGLE_", "GEMINI_", "GCLOUD_", "AZURE_", "SPEECH_",
            "ELEVENLABS_", "AWS_", "BOTO_", "NTFY_", "SQUABBIT_", "SUPERSAAS_", "OBS_", "TYPESAFE_")
startup: Profile | None = None       # the pairing Shotcast started in (set once by __main__): None, self-hosted


class CloudError(Exception):
    """Something to tell the user; the message says what happened."""


class NotPaired(CloudError):
    pass


class Refused(NotPaired):
    """The API turned this PC's pass down (401 on a refresh): the pairing is kept, marked, till it's
    paired again."""


class Damaged(CloudError):
    """This PC's pairing or its profile can't be trusted: nothing starts (`locate`)."""

    def __init__(self, problem: str) -> None:
        self.problem = problem
        super().__init__(f"This PC's Shotcast Cloud pairing can't be used: {problem}. {FIX}")


class Unreachable(CloudError):
    """Shotcast Cloud couldn't be reached (the network), or stayed busy: tried again later."""


class PairingChanged(CloudError):
    """The PC was unpaired or paired elsewhere while an operation was under way: it stops, so no
    request mixes one pairing's work with another's token or address."""


class TooOld(CloudError):
    """The API needs a newer Shotcast (426)."""

    def __init__(self, minimum: str | None) -> None:
        self.minimum = minimum or "a newer version"
        super().__init__(f"Shotcast Cloud needs Shotcast {self.minimum} or later: update Shotcast")


# ---- where things are -----------------------------------------------------------------------

def root() -> Path:
    """The machine's Shotcast Cloud folder, which no config or command line moves, on this machine's
    own disk (never a roaming profile's): %LOCALAPPDATA%\\Shotcast\\cloud on Windows, ~/Library/
    Application Support/Shotcast/cloud on a Mac, else ~/.config/Shotcast/cloud. ROOT_ENV moves it (tests)."""
    if got := os.environ.get(ROOT_ENV):
        return Path(got)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Shotcast" / "cloud"


def marker() -> Path:
    """This PC's pairing: its being there is what makes the PC paired."""
    return root() / MARKER


def store() -> TokenStore:
    return TokenStore(marker())


@cache
def machine_id() -> str:
    """This machine, as a pairing is bound to it: Windows' MachineGuid, a Mac's hardware UUID, else
    /etc/machine-id (the network card's address if none of those can be read), hashed."""
    got = ""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                got = str(winreg.QueryValueEx(k, "MachineGuid")[0])
        elif sys.platform == "darwin":
            import subprocess

            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True,
                                 text=True, timeout=10).stdout
            found = re.search(r'"IOPlatformUUID" = "([^"]+)"', out)
            got = found[1] if found else ""
        else:
            for p in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
                if p.is_file() and (got := p.read_text(encoding="utf-8").strip()):
                    break
    except Exception:   # noqa: BLE001 - the fallback below
        got = ""
    if not got:
        import uuid

        got = f"node:{uuid.getnode():012x}"
    return hashlib.sha256(f"shotcast-machine:{got}".encode()).hexdigest()[:32]


def clean_env() -> list[str]:
    """Paired, as Shotcast starts: drop the service keys and endpoint overrides it inherited (SCRUBBED),
    so only the profile's .env sets them. Returns the names dropped."""
    gone = [k for k in os.environ if k.upper().startswith(SCRUBBED)]
    for k in gone:
        os.environ.pop(k, None)
    return gone


def canon(api: str) -> str:
    """An API origin as it's kept and compared: lower-case scheme and host, no default port, no
    trailing slash."""
    u = urlsplit(str(api).strip())
    scheme, host = (u.scheme or "").lower(), (u.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = u.port
    except ValueError:
        port = None
    keep = port is not None and port != {"https": 443, "http": 80}.get(scheme)
    path = u.path.rstrip("/")
    return f"{scheme}://{host}{f':{port}' if keep else ''}{path}"


def pairing() -> dict[str, Any]:
    """The marker's clear part (no tokens); empty unpaired or unreadable."""
    try:
        rec = json.loads(marker().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(rec, dict):
        return {}
    rec.pop("secret", None)
    return rec


def identity(rec: dict[str, Any]) -> tuple:
    """Which pairing a record is: its API, tenant, device and bay. Work started under one is never
    finished under another."""
    return (canon(rec.get("api") or ""), rec.get("tenant_id"), rec.get("device_id"), bay_of(rec))


def bay_of(rec: dict[str, Any]) -> str | None:
    """The pairing's bay: its server id when the API gives one, else its exact name on the tenant as
    the API returned it (the API keeps bay names unique per tenant, ignoring case; the name is used
    as returned, never folded)."""
    if rec.get("bay_id"):
        return f"id:{rec['bay_id']}"
    return f"name:{rec['sim_name']}" if rec.get("sim_name") is not None else None


def _safe(text: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text))[:40] or "_"


def folder_key(rec: dict[str, Any]) -> tuple:
    """What a profile is for: the pairing's API, tenant and device. (Shotcast Cloud binds a device to
    one bay for good, so the device is the bay; a bay renamed on the site is the same profile.)"""
    return identity(rec)[:3]


def home_for(key: tuple | list) -> Path:
    """A pairing's profile home: root()/<tenant>-<device>-<hash of folder_key>. Pairing again (another
    device), or to another API or tenant, is another profile."""
    key = list(key)
    digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:10]
    return root() / f"{_safe(key[1])}-{_safe(key[2])}-{digest}"


def folder_identity(home: str | Path) -> tuple | None:
    """The folder_key a profile home was made for, or None if it isn't one."""
    try:
        got = json.loads((Path(home) / FOLDER_FILE).read_text(encoding="utf-8"))["key"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return tuple(got) if isinstance(got, list) else None


@dataclass
class Profile:
    """This PC's pairing and its cloud profile, as checked (`inspect`): its home, the marker's clear
    part and the verified, protected binding (`bound`: the API origin, tenant and device its tokens
    were issued for). `damage`: why it can't be used (nothing starts; `home` may be None). `problem`:
    it's sound but the API no longer accepts it (a refused refresh): the booth captures in it and
    sends nothing."""
    home: Path | None
    rec: dict[str, Any] = field(default_factory=dict)
    bound: tuple | None = None
    problem: str | None = None
    damage: str | None = None

    @property
    def connected(self) -> bool:
        return self.problem is None and self.damage is None

    @property
    def origin(self) -> str | None:
        """The verified API origin (the protected binding's, never the marker's clear part)."""
        return self.bound[0] if self.bound and not self.damage else None

    @property
    def config(self) -> Path:
        return self.home / "config.yaml"

    @property
    def env(self) -> Path:
        return self.home / ".env"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def db(self) -> Path:
        return self.logs / "season.db"

    @property
    def paired_at(self) -> float | None:
        got = self.rec.get("paired_at")
        return float(got) if isinstance(got, (int, float)) and not isinstance(got, bool) else None


def _db_binding(db: Path) -> tuple | None:
    """The pairing a profile's season file is bound to (cloud_rounds.bind), read only. Raises
    sqlite3.Error for a file that isn't a season file, or is damaged."""
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        con.execute("SELECT COUNT(*) FROM round_records").fetchone()
        row = con.execute("SELECT api, tenant_id, device_id FROM cloud_pairing WHERE one = 1").fetchone()
    finally:
        con.close()
    return tuple(row) if row else None


class _Bad(Exception):
    pass


def _text(rec: dict[str, Any], name: str, optional: bool = False) -> None:
    v = rec.get(name)
    if not (isinstance(v, str) and v) and not (optional and v is None):
        raise _Bad(f"its {name} isn't valid")


def _verify_marker(path: Path) -> tuple[dict[str, Any], tuple]:
    """(clear part, protected binding) of a marker that can be trusted; else _Bad (why)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise _Bad(f"{path} can't be read ({e})") from None
    if not isinstance(raw, dict):
        raise _Bad(f"{path} isn't a pairing")
    if raw.get("v") != STORE_VERSION:
        raise _Bad(f"{path} is from another version of Shotcast")
    clear = {k: v for k, v in raw.items() if k != "secret"}
    for name in ("api", "tenant_id", "device_id", "sim_name"):
        _text(clear, name)
    for name in ("league_id", "bay_id"):
        _text(clear, name, optional=True)
    try:
        secret = json.loads(unprotect(raw["secret"]))
        bound, machine = secret["bind"], secret["machine"]
        if not (isinstance(bound, list) and len(bound) == 3 and all(isinstance(x, str) and x for x in bound)):
            raise ValueError("no binding")
    except Exception as e:   # noqa: BLE001 - a damaged file, or another Windows user's
        raise _Bad(f"this PC's Shotcast Cloud pass can't be read ({e})") from None
    if canon(bound[0]) != bound[0] or binding(clear) != bound:
        raise _Bad(f"{path} was changed outside Shotcast (its address, tenant or device isn't the one its "
                   "pass was issued for)")
    if machine != machine_id():
        raise _Bad(f"{path} was paired on another computer")
    return clear, tuple(bound)


def _verify_profile(home: Path, bound: tuple) -> None:
    """That the pairing's profile is there, its own, with its season file bound to it and a config.yaml
    that loads; else _Bad (why). Nothing is made or changed here: that's pairing's (make_profile)."""
    from .config import load_config

    if not home.is_dir():
        raise _Bad(f"its Shotcast profile ({home}) is missing")
    if folder_identity(home) != bound:
        raise _Bad(f"{home} was made for another Shotcast Cloud pairing")
    db = home / "logs" / "season.db"
    if not db.is_file():
        raise _Bad(f"its season file ({db}) is missing")
    try:
        got = _db_binding(db)
    except sqlite3.Error as e:
        raise _Bad(f"its season file ({db}) can't be read ({e})") from None
    if got != bound:
        raise _Bad(f"{db} isn't this Shotcast Cloud pairing's")
    if not (home / "config.yaml").is_file():
        raise _Bad(f"its config.yaml ({home / 'config.yaml'}) is missing")
    try:
        load_config(home / "config.yaml")
    except Exception as e:   # noqa: BLE001 - not YAML, or a setting that isn't valid
        raise _Bad(f"its config.yaml ({home / 'config.yaml'}) isn't valid ({e})") from None


def inspect() -> Profile | None:
    """This PC's pairing as it stands: None with no marker (the self-hosted Shotcast), else its Profile,
    with `damage` set when it can't be trusted. Every field is checked here, so a damaged marker is
    damage, never an error further on. Reads only: nothing is made."""
    path = marker()
    try:
        path.stat()
    except FileNotFoundError:
        return None
    except OSError as e:
        return Profile(None, damage=f"{path} can't be read ({e})")
    clear: dict[str, Any] = {}
    home = None
    try:
        clear, bound = _verify_marker(path)
        home = home_for(bound)
        _verify_profile(home, bound)
    except _Bad as e:
        return Profile(home, clear, damage=str(e))
    except Exception as e:   # noqa: BLE001 - anything else about it is damage too
        return Profile(home, clear, damage=f"{path} can't be used ({e})")
    return Profile(home, clear, bound, problem=REPAIR if clear.get("refused") else None)


def locate() -> Profile | None:
    """The cloud profile to run in, or None: no marker, so this is the self-hosted Shotcast. Checked
    before anything is logged or captured. Raises Damaged for a pairing that can't be trusted: nothing
    starts then, and nothing falls back to the self-hosted Shotcast."""
    prof = inspect()
    if prof is not None and prof.damage:
        raise Damaged(prof.damage)
    return prof


def _lifecycle(home: Path, who: str, timeout: float):
    from .locks import file_lock

    return file_lock(home / f"{who}.lock", timeout, "Shotcast is running with this pairing: it holds")


@contextmanager
def running(prof: Profile, who: str):
    """Held while a paired booth (`run`) or tray (`tray`) runs in the profile, so it isn't unpaired from
    under it (`unpair` without --force refuses meanwhile)."""
    with _lifecycle(prof.home, who, 30):
        yield


# ---- making a profile -----------------------------------------------------------------------

def seed_config(cfg: Any) -> dict[str, Any]:
    """A profile's config.yaml: the CARRIED settings of `cfg`, less the private bits (the news feeds
    added, the shed's location: weather is off, as it needs one; set it in the profile to have it)."""
    data = cfg.model_dump(mode="json", include=set(CARRIED))
    data.get("news", {}).pop("feeds", None)
    weather = data.get("weather", {})
    for k in ("location", "country", "place", "latitude", "longitude"):
        weather.pop(k, None)
    weather["enabled"] = False
    return data


def seed_env(paths: list[Path]) -> dict[str, str]:
    """The ENV_KEYS set in these .env files (the later file's win), and nothing else from them."""
    from .report import env_pairs

    out: dict[str, str] = {}
    for p in paths:
        try:
            out.update((k, v) for k, v in env_pairs(p) if k in ENV_KEYS and v)
        except OSError:
            pass
    return out


def _write_new(path: Path, text: str, mode: int = 0o644) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def make_profile(key: tuple | list, rec: dict[str, Any], seed: dict[str, Any] | None = None,
                 env: dict[str, str] | None = None) -> Path:
    """Make a pairing's profile home, as it's paired (only then: startup never makes one), one process
    at a time and checked again under the lock: pairing.json, config.yaml (`seed`, else the defaults'
    CARRIED settings), .env (`env`) and logs/season.db bound to the pairing. What's there already is
    kept: nothing is replaced."""
    import yaml

    from .config import Config
    from .locks import file_lock

    home = home_for(key)
    root().mkdir(parents=True, exist_ok=True)
    with file_lock(root() / f"{home.name}.lock", 120.0, "another process has held"):
        home.mkdir(parents=True, exist_ok=True)
        mark = home / FOLDER_FILE
        if not mark.exists():
            _write_new(mark, json.dumps({"key": list(key), "sim_name": rec.get("sim_name"),
                                         "made_at": time.time()}, indent=1))
        elif folder_identity(home) != tuple(key):
            raise CloudError(f"{home} was made for another Shotcast Cloud pairing")
        if not (home / "config.yaml").exists():
            body = yaml.safe_dump(seed if seed is not None else seed_config(Config()), sort_keys=False,
                                  allow_unicode=True)
            _write_new(home / "config.yaml", "# Shotcast Cloud profile: this PC's booth while it's paired.\n" + body)
        if not (home / ".env").exists():
            _write_new(home / ".env", "".join(f"{k}={v}\n" for k, v in (env or {}).items()), 0o600)
        logs = home / "logs"
        logs.mkdir(exist_ok=True)
        if not (logs / "season.db").exists():
            _new_season(logs, key)
    return home


def _new_season(logs: Path, key: tuple | list) -> None:
    """An empty season file bound to the pairing before it's the season file, so it's never there unbound."""
    from . import cloud_rounds
    from .store import Store

    new = logs / "season.db.new"
    for leftover in logs.glob("season.db.new*"):
        leftover.unlink()
    Store(str(new), new_session=False).db.close()
    cloud_rounds.bind(str(new), tuple(key))
    one = sqlite3.connect(new)
    try:   # (one file, with nothing of it left in a WAL beside it, to rename; the booth makes it WAL again)
        one.execute("PRAGMA journal_mode = DELETE")
    finally:
        one.close()
    for side in logs.glob("season.db.new-*"):
        side.unlink()
    if (logs / "season.db").exists():
        raise CloudError(f"{logs / 'season.db'} appeared meanwhile; it's kept")
    os.replace(new, logs / "season.db")


def booth_config(cfg: Any, prof: Profile | None) -> Any:
    """The config a process runs with: `cfg` itself unpaired (prof None). Paired, one built from
    scratch: the profile config's CARRIED settings, every other one its default, working in the
    profile's home with no league (every round practice), and the self-hosted booth's jobs off: the
    stats page, publishing, the league page and recaps, S3 backups, the live stream and recordings,
    the site table's chat and alerts, Squabbit, bookings and phone pushes."""
    if prof is None:
        return cfg
    src = cfg.model_copy(deep=True)
    out = type(cfg)()
    for name in CARRIED:
        setattr(out, name, getattr(src, name))
    out.log_dir = str(prof.logs)                 # logs, prompt logs, screens, round files, problem reports
    out.db_path = str(prof.db)
    out.history_file = str(prof.home / "data" / "history.json")
    out.league_file = None
    out.publish.enabled, out.publish.bucket, out.publish.recaps = False, None, False
    out.backup.s3, out.backup.dir = False, None                       # (local copies, in the profile)
    out.live.enabled, out.live.with_rounds, out.live.launch_obs, out.live.local_keep_days = False, False, False, 0
    out.chat.enabled, out.chat.table, out.chat.alerts = False, None, False
    out.squabbit.enabled = out.booking.enabled = False
    out.notify.provider = "none"
    return out


def web_stats_url(prof: Profile | None) -> str | None:
    """The stats page on Shotcast Cloud for a pairing's default league (a home tenant's league), at its
    verified API origin (Profile.origin) only: none for a pairing that can't be trusted."""
    if prof is None or not prof.origin:
        return None
    league = prof.rec.get("league_id")
    if not isinstance(league, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", league):
        return None
    return f"{prof.origin}/leagues/{league}/"


def check_api(api: str) -> str:
    """The API's origin (canon): https, or http only to this computer."""
    raw = api.strip()
    u = urlsplit(raw)
    local = u.hostname in ("localhost", "127.0.0.1", "::1")
    try:
        u.port
    except ValueError:
        raise CloudError(f"{api!r} isn't a Shotcast Cloud address (https://host)") from None
    if not u.hostname or u.scheme.lower() not in ("https", "http") or (u.scheme.lower() == "http" and not local) \
            or u.path.strip("/") or u.query or u.fragment or u.username or u.password:
        raise CloudError(f"{api!r} isn't a Shotcast Cloud address (https://host)")
    return canon(raw)


def show_fingerprint(fingerprint: str) -> str:
    """As the pairing page shows it, so the owner can match the two."""
    return f"{fingerprint[:4]} {fingerprint[4:8]}".upper()


# ---- the token store ------------------------------------------------------------------------

def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32   # type: ignore[attr-defined]
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    def blob(b: bytes) -> tuple[Blob, Any]:
        buf = ctypes.create_string_buffer(b, len(b))
        return Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    src, _keep = blob(data)
    ent, _keep2 = blob(_ENTROPY)
    out = Blob()
    UI_FORBIDDEN = 0x1
    if not fn(ctypes.byref(src), "Shotcast Cloud" if protect else None, ctypes.byref(ent), None, None,
              UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError()   # type: ignore[attr-defined]
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> str:
    """Encrypt for this Windows user (DPAPI). Elsewhere (development) it's only encoded; the file is
    still readable by its owner alone."""
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(data, True)).decode()
    return "plain:" + base64.b64encode(data).decode()


def unprotect(text: str) -> bytes:
    kind, _, body = text.partition(":")
    raw = base64.b64decode(body)
    if kind == "dpapi" and sys.platform == "win32":
        return _dpapi(raw, False)
    if kind == "plain":
        return raw
    raise CloudError("this PC's Shotcast Cloud pairing can't be read here; pair it again")


def binding(rec: dict[str, Any]) -> list:
    """What a pairing's tokens were issued for: its API, tenant and device (folder_key)."""
    return list(folder_key(rec))


class TokenStore:
    """This PC's pairing in one JSON file: who it is (tenant, bay, device, API) in the clear, and,
    protected, the tokens with the API, tenant and device they were issued for. A clear part that no
    longer says the same (an edit to its `api`, say) is refused, so the tokens never go elsewhere.
    Every read-modify-write happens under `lock()`, across processes; writes are atomic."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def lock(self, timeout: float = 120.0):
        from .locks import file_lock

        return file_lock(self.path.with_name(self.path.name + ".lock"), timeout, "another process has held")

    def read(self) -> dict | None:
        try:
            rec = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise CloudError(f"couldn't read {self.path} ({e}); pair this PC again") from None
        if not isinstance(rec, dict) or rec.get("v") != STORE_VERSION:
            raise CloudError(f"{self.path} is from another version; pair this PC again")
        if not all(isinstance(rec.get(k), str) and rec[k] for k in ("api", "tenant_id", "device_id")):
            raise CloudError(f"{self.path} isn't a valid pairing; pair this PC again")
        try:
            secret = json.loads(unprotect(rec.pop("secret")))
            rec["tokens"], bound, machine = secret["tokens"], secret["bind"], secret["machine"]
            same = binding(rec) == bound
        except CloudError:
            raise
        except Exception as e:   # noqa: BLE001 - a damaged file
            raise CloudError(f"couldn't read {self.path} ({e}); pair this PC again") from None
        if not same:
            raise PairingChanged(f"{self.path} was changed outside Shotcast (its address, tenant or device isn't "
                                 "the one its pass was issued for); pair this PC again")
        if machine != machine_id():
            raise PairingChanged(f"{self.path} was paired on another computer; pair this PC again")
        return rec

    def write(self, rec: dict) -> None:
        body = {k: v for k, v in rec.items() if k != "tokens"}
        body["v"] = STORE_VERSION
        body["secret"] = protect(json.dumps({"tokens": rec["tokens"], "bind": binding(rec),
                                             "machine": machine_id()}).encode())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=1)
        os.replace(tmp, self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


# ---- talking to the API ---------------------------------------------------------------------

def _http(api: str, http: Any = None) -> Any:
    if http is not None:
        return http
    import httpx

    from .updater import current_version

    return httpx.Client(base_url=api, timeout=30.0, follow_redirects=False,
                        headers={"User-Agent": f"Shotcast/{current_version()}"})


def _message(r: Any) -> str:
    """The API's own words for a refusal."""
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error") or detail)
    if isinstance(detail, list):   # a validation error
        return "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail)


def _send(http: Any, method: str, path: str, body: Any = None, headers: dict | None = None,
          params: dict | None = None, timeout: float | None = None, raw: bytes | None = None) -> Any:
    """`body` is sent as JSON; `raw`, bytes already encoded (with the caller's Content-Type)."""
    data = raw if raw is not None else b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
    h = dict(headers or {})
    if method != "GET":
        h[BODY_HASH] = hashlib.sha256(data).hexdigest()
        if body is not None:
            h["Content-Type"] = "application/json"
    extra = {} if timeout is None else {"timeout": timeout}
    import httpx

    try:
        return http.request(method, path, content=data if method != "GET" else None, headers=h, params=params,
                            **extra)
    except (httpx.TransportError, OSError) as e:   # (the network: no answer at all)
        raise Unreachable(f"couldn't reach Shotcast Cloud: {e}") from None


def _fresh(got: dict) -> dict:
    """What a token answer says about the pairing (the bay's id only if the API gives one)."""
    out = {"league_id": got.get("league_id"), "tenant_id": got["tenant_id"], "sim_name": got["sim_name"]}
    if got.get("bay_id"):
        out["bay_id"] = got["bay_id"]
    return out


class Cloud:
    """The paired PC's view of the API: device-authenticated requests, refreshing as needed. Bound to
    the pairing it was made for (`identity`): once the stored pairing is another, every request it
    would make raises PairingChanged. `expect`: the protected binding it must be (a Profile's
    `bound`, as checked), else PairingChanged. The network's failures raise Unreachable."""

    def __init__(self, store: TokenStore, http: Any = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, expect: tuple | None = None) -> None:
        self.store, self.clock, self.sleep = store, clock, sleep
        rec = store.read()
        if rec is None:
            raise NotPaired("this PC isn't paired with Shotcast Cloud")
        if expect is not None and tuple(binding(rec)) != tuple(expect):
            raise PairingChanged("this PC's Shotcast Cloud pairing isn't the one checked; nothing sent")
        if rec.get("refused"):
            raise Refused(REPAIR)
        self.device = {k: v for k, v in rec.items() if k != "tokens"}
        self.identity = identity(self.device)
        self.bound = binding(rec)   # (the protected binding: TokenStore.read checked the clear part against it)
        self.http = _http(self.bound[0], http)

    def _send(self, method: str, path: str, *args: Any, **kw: Any) -> Any:
        """Every request that carries a token: only to the API the tokens were issued for."""
        base = canon(str(getattr(self.http, "base_url", "") or ""))
        if base != canon(self.bound[0]):
            raise PairingChanged(f"this PC's Shotcast Cloud pass is for {self.bound[0]}, not {base or 'this address'}; "
                                 "nothing sent")
        return _send(self.http, method, path, *args, **kw)

    @property
    def league(self) -> str | None:
        """The tenant's default league: a home tenant's one league; often none at a venue."""
        return self.device.get("league_id")

    def _mine(self) -> dict:
        """The stored pairing, read under the store's lock, if it's still this one."""
        rec = self.store.read()
        if rec is None:
            raise NotPaired("this PC was unpaired from Shotcast Cloud")
        if identity(rec) != self.identity or binding(rec) != self.bound:
            raise PairingChanged("this PC's Shotcast Cloud pairing changed meanwhile; stopped")
        return rec

    def access_token(self, stale: str | None = None) -> str:
        """A current access token for this pairing. `stale`: one the API just refused, so a fresh one
        is needed even if it hasn't run out (unless another process has already replaced it)."""
        with self.store.lock():
            rec = self._mine()
            tok = rec["tokens"]
            if tok["access_token"] != stale and tok["access_exp"] - REFRESH_EARLY_S > self.clock():
                return tok["access_token"]
            try:
                got = self._refresh(tok["refresh_token"])
            except Refused:
                rec["refused"] = self.clock()   # kept, marked: `locate` says it needs pairing again
                self._save(rec)
                raise
            rec["tokens"] = {"refresh_token": got["refresh_token"], "access_token": got["access_token"],
                             "access_exp": self.clock() + got["access_expires_in"]}
            fresh = _fresh(got)
            rec |= fresh
            self._save(rec)   # before using it (or not): the old refresh token is spent
            if identity(rec) != self.identity:
                raise PairingChanged("Shotcast Cloud now has this PC as another bay or tenant; stopped")
            self.device |= fresh
            return got["access_token"]

    def _save(self, rec: dict) -> None:
        # The store holds the only copy of the new refresh token, so a passing failure (antivirus
        # holding the file, say) is retried rather than losing it.
        for attempt in range(6):
            try:
                self.store.write(rec)
                return
            except OSError as e:
                if attempt == 5:
                    raise CloudError(f"couldn't save this PC's new Shotcast Cloud pass: {e}") from None
                self.sleep(0.5 * (attempt + 1))

    def _refresh(self, refresh_token: str) -> dict:
        # A lost response is safe to retry, but only within the API's grace for a spent token (a
        # minute, during which it hands back the same new one), so every try fits a shorter window.
        deadline = self.clock() + REFRESH_WINDOW_S
        attempt = 0
        while True:
            try:
                r = self._send("POST", "/api/devices/refresh", {"refresh_token": refresh_token},
                          timeout=min(REFRESH_TIMEOUT_S, max(deadline - self.clock(), 1.0)))
            except Unreachable as e:
                problem = str(e)
            else:
                # A 5xx can come from a proxy after the API rotated it, so it's retried like a lost answer.
                if r.status_code < 500 and r.status_code != 429:
                    break
                problem = f"Shotcast Cloud couldn't refresh this PC's pass: {_message(r)}"
            attempt += 1
            wait = 2.0 * attempt
            if self.clock() + wait + 1.0 >= deadline:
                raise Unreachable(problem)
            self.sleep(wait)
        _too_old(r)
        if r.status_code == 401:
            raise Refused(REPAIR)
        try:
            got = r.json() if r.status_code == 200 else None
        except ValueError:
            got = None
        if not isinstance(got, dict) or not all(isinstance(got.get(k), str) for k in ("refresh_token", "access_token")):
            raise CloudError(f"Shotcast Cloud refused to refresh this PC's pass: {_message(r)}")
        return got

    def request(self, method: str, path: str, body: Any = None, headers: dict | None = None,
                params: dict | None = None, raw: bytes | None = None) -> Any:
        token = self.access_token()
        r = self._send(method, path, body, {**(headers or {}), DEVICE_HEADER: token}, params, raw=raw)
        if r.status_code == 401:   # revoked meanwhile, or the clock is off: refresh once and retry
            token = self.access_token(stale=token)
            r = self._send(method, path, body, {**(headers or {}), DEVICE_HEADER: token}, params, raw=raw)
        _too_old(r)
        return r

    def me(self) -> dict:
        r = self.request("GET", "/api/devices/me")
        try:
            got = r.json() if r.status_code == 200 else None
        except ValueError:
            got = None
        if not isinstance(got, dict):
            raise CloudError(f"Shotcast Cloud: {_message(r)}")
        return got

    def revoke(self) -> None:
        """Remove this PC from its tenant on the API (the pairing here is kept: `forget`). Only the API's
        204 confirms it: anything else, a refused refresh (Refused) too, raises."""
        r = self.request("DELETE", "/api/devices/me")
        if r.status_code != 204:
            raise CloudError(f"Shotcast Cloud didn't confirm removing this PC: {_message(r)}")


def forget(st: TokenStore, bound: tuple | None) -> None:
    """Delete this PC's pairing (its tokens), if it's still the one bound to `bound` (None: whatever is
    there, for a pairing that can't be read)."""
    with st.lock():
        if bound is not None:
            try:
                rec = st.read()
            except CloudError:
                rec = None
            if rec is not None and tuple(binding(rec)) != tuple(bound):
                raise PairingChanged("this PC was paired again meanwhile; that pairing is kept")
        st.delete()


def _too_old(r: Any) -> None:
    if r.status_code == 426:
        raise TooOld(r.headers.get(MIN_VERSION_HEADER))


# ---- pairing --------------------------------------------------------------------------------

def pair(store: TokenStore, api: str, name: str | None = None, http: Any = None,
         clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
         say: Callable[[str], None] = print, prepare: Callable[[dict], Any] | None = None) -> dict:
    """Pair this PC: show a code for the owner to enter on the site, and wait for them to approve it.
    `prepare(rec)` runs once it's approved, before the pairing is stored (the CLI makes the profile
    then, so the marker is never there without it). Returns the stored pairing (without its tokens)."""
    api = check_api(api)
    with store.lock():
        if store.path.exists():
            raise CloudError("this PC is already paired; unpair it first")
    name = (name or socket.gethostname() or "Sim PC").strip()[:200]
    fingerprint = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    http = _http(api, http)
    r = _send(http, "POST", "/api/devices/pair", {"name": name, "fingerprint": fingerprint})
    _too_old(r)
    if r.status_code != 201:
        wait = r.headers.get("Retry-After")
        raise CloudError(f"Shotcast Cloud didn't start pairing: {_message(r)}"
                         + (f" (try again in {wait}s)" if wait else ""))
    start = r.json()
    say(f"On a phone or computer signed in to Shotcast Cloud as the owner, open\n"
        f"  {start['verification_uri_complete']}\n"
        f"and check the code {start['user_code']} and this PC's fingerprint {show_fingerprint(fingerprint)}.\n"
        f"Waiting for approval (the code lasts {start['expires_in'] // 60} minutes)...")
    interval = max(1, int(start["interval"]))
    deadline = clock() + start["expires_in"]
    while clock() < deadline:
        sleep(interval)
        try:
            r = _send(http, "POST", "/api/devices/token", {"device_code": start["device_code"]})
        except Unreachable:   # (the network: keep polling)
            continue
        _too_old(r)
        if r.status_code == 200:
            got = r.json()
            rec = {"api": api, "device_id": got["device_id"], **_fresh(got), "name": name,
                   "fingerprint": fingerprint,
                   "paired_at": time.time(),
                   "tokens": {"refresh_token": got["refresh_token"], "access_token": got["access_token"],
                              "access_exp": clock() + got["access_expires_in"]}}
            with store.lock():
                if store.path.exists():   # another pairing finished while this one waited
                    raise CloudError("this PC was paired meanwhile by another pairing; this one wasn't "
                                     "kept (remove it from the sims on the site)")
                if prepare is not None:
                    prepare(rec)
                store.write(rec)
            return {k: v for k, v in rec.items() if k != "tokens"}
        error = _error(r)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise CloudError("the owner turned this PC down")
        if error == "expired_token":
            raise CloudError("the code ran out before it was approved; start again")
        if r.status_code >= 500 or r.status_code == 429:
            continue
        raise CloudError(f"Shotcast Cloud: {_message(r)}")
    raise CloudError("the code ran out before it was approved; start again")


def _error(r: Any) -> str | None:
    if r.status_code != 400:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


# ---- the command line -----------------------------------------------------------------------

def _default_league(d: dict) -> str:
    return f"with default league {d['league_id']}" if d.get("league_id") else "with no default league"


def _self_hosted_files(config: str | None) -> tuple[Path | None, list[Path]]:
    """The self-hosted config.yaml to seed a profile from, and its .env files (the booth's own: beside
    the code and beside config.yaml), read only here, as the PC is paired. `config` None: the one in
    this folder, else the Windows app's."""
    from .tray import default_home

    if config is not None:
        path = Path(config)
    else:
        path = Path("config.yaml")
        if not path.is_file():
            path = default_home() / "config.yaml"
    envs = [Path(__file__).resolve().parents[1] / ".env", path.resolve().parent / ".env"]
    return (path if path.is_file() else None), list(dict.fromkeys(envs))


def _seed(config: str | None) -> tuple[dict, dict]:
    from .config import Config, load_config

    path, envs = _self_hosted_files(config)
    cfg = load_config(path) if path is not None else Config()
    return seed_config(cfg), seed_env(envs)


def _describe(items: list[tuple[str, str]]) -> str:
    return "\n".join(f"  {uuid}: {what}" for uuid, what in items)


def main(argv: list[str] | None = None) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="python -m commentator.cloud", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pair", help="Pair this PC with Shotcast Cloud as one of a tenant's sims")
    s.add_argument("--api", required=True, help="the site's address, e.g. https://app.shotcastgolf.com")
    s.add_argument("--name", help="what the owner sees this PC as (default: its computer name)")
    s.add_argument("--config", help="the self-hosted config.yaml to start the profile's settings from "
                                    "(default: this folder's, else the Windows app's)")
    sub.add_parser("status", help="Which tenant and sim this PC is, and how its rounds stand")
    s = sub.add_parser("upload", help="Send this PC's settled rounds now")
    s.add_argument("--settle", type=float, help=argparse.SUPPRESS)   # (tests: don't wait out the closer)
    s = sub.add_parser("unpair", help="Stop using Shotcast Cloud and remove this PC from its tenant")
    s.add_argument("--force", action="store_true",
                   help="unpair even with rounds not yet on Shotcast Cloud, or if it can't be reached")
    a = p.parse_args(argv)
    st = store()
    restart = "Restart Shotcast to apply."
    try:
        if a.cmd == "pair":
            if inspect() is not None:
                sys.exit("This PC is already paired (or its pairing is damaged): unpair it first.")
            seed, env = _seed(a.config)
            d = pair(st, a.api, a.name, prepare=lambda rec: make_profile(folder_key(rec), rec, seed, env))
            print(f"Paired as {d['name']!r} (sim {d['sim_name']!r}) {_default_league(d)}; its Shotcast is in "
                  f"{home_for(folder_key(d))}, with {', '.join(env) or 'no API keys'} from this PC's .env. "
                  + restart)
            return
        prof = inspect()   # (damaged, it can still be looked at and unpaired)
        if prof is None:
            sys.exit("This PC isn't paired with Shotcast Cloud.")
        if a.cmd == "status":
            _status(prof)
        elif a.cmd == "upload":
            from . import cloud_rounds

            if prof.damage:
                raise Damaged(prof.damage)
            if prof.problem:
                sys.exit(f"Nothing sent: {prof.problem}")
            cl = Cloud(st, expect=prof.bound)
            try:
                out = cloud_rounds.drain(cl, str(prof.db), **({} if a.settle is None else {"settle": a.settle}))
            except (cloud_rounds.Blocked, cloud_rounds.NotThisPairing, TimeoutError) as e:
                sys.exit(str(e))
            print(f"Sent {len(out.sent)} round(s); {out.unchanged} unchanged, {out.waiting} to retry, "
                  f"{len(out.conflicts)} held as conflicts, {len(out.parked)} refused")
        elif a.cmd == "unpair":
            _unpair(prof, st, a.force)
            print("Unpaired: this PC is a self-hosted booth again, with its own data"
                  + (f" (the profile is kept in {prof.home})" if prof.home else "") + ". " + restart)
    except CloudError as e:
        sys.exit(str(e))


def _local(prof: Profile) -> list[tuple[str, str]] | None:
    """The profile's rounds not yet on Shotcast Cloud, read here (None when its season file can't be)."""
    from . import cloud_rounds

    if prof.home is None or not prof.db.is_file():
        return None
    try:
        return cloud_rounds.outstanding(str(prof.db))
    except Exception:   # noqa: BLE001 - a damaged season file
        return None


def _status(prof: Profile) -> None:
    from . import cloud_rounds

    if prof.damage:
        print(f"Paired, but the pairing can't be used: {prof.damage}. {FIX}")
    elif prof.problem:
        print(f"Paired, but disconnected: {prof.problem}")
    else:
        try:
            me = Cloud(store(), expect=prof.bound).me()
            print(f"Paired as {me.get('name')!r} (sim {me.get('sim_name')!r}) {_default_league(me)}"
                  + ("; the league is suspended" if me.get("league_active") is False else ""))
        except CloudError as e:
            print(f"Paired as {prof.rec.get('name')!r} (sim {prof.rec.get('sim_name')!r}); "
                  f"Shotcast Cloud didn't answer: {e}")
    print(f"Profile: {prof.home or 'unknown'}")
    if prof.home is not None and prof.db.is_file():
        try:
            q = cloud_rounds.status(str(prof.db))
            left = cloud_rounds.outstanding(str(prof.db))
        except Exception as e:   # noqa: BLE001 - a damaged season file
            print(f"Rounds: its season file can't be read ({e})")
            return
        print("Rounds: " + ", ".join(f"{q[k]} {what}" for k, what in cloud_rounds.STATUS_WORDS))
        for uuid, what in left:
            if what in ("conflict", "refused"):
                print(f"  {uuid}: {what}")


def _unpair(prof: Profile, st: TokenStore, force: bool) -> None:
    """Without `force`: refuses while Shotcast runs with this pairing (its lifecycle lock), and, under
    that lock, unless it's still the pairing checked and every round is acknowledged; then the API
    must confirm removing the PC (204) before the pairing is forgotten here. With `force`: says what's
    left behind, and forgets it here whatever the API said (or a damaged pairing)."""
    if prof.damage:
        if not force:
            sys.exit(f"Not unpaired: {prof.damage}. Pass --force to forget this PC's pairing here.")
        left = _local(prof)
        if left:
            print(f"Left on this PC only, never sent ({prof.home}):\n{_describe(left)}")
        print("The pairing couldn't be used, so Shotcast Cloud wasn't asked to remove this PC: remove it "
              "from the sims on the site.")
        forget(st, None)
        return
    with ExitStack() as held:
        if not force:
            try:
                for who in LIFECYCLE:
                    held.enter_context(_lifecycle(prof.home, who, 0))
            except TimeoutError:
                sys.exit("Not unpaired: Shotcast is running with this pairing; stop Shotcast first.")
            now = inspect()
            if now is None or now.damage or now.bound != prof.bound:
                sys.exit("Not unpaired: this PC's pairing changed meanwhile; run it again.")
        left = _local(prof)
        if left is None:
            left = [("?", "the season file can't be read")]
        if left and not force:
            sys.exit(f"Not unpaired: {len(left)} round(s) aren't on Shotcast Cloud yet:\n{_describe(left)}\n"
                     "Let them upload (cloud upload), or pass --force to unpair and leave them on this PC only.")
        try:
            Cloud(st, expect=prof.bound).revoke()
        except Refused:
            if not force:
                sys.exit("Not unpaired: Shotcast Cloud no longer accepts this PC, so it can't confirm removing "
                         "it. Remove this PC from the sims on the site, then unpair with --force.")
            print("Shotcast Cloud no longer accepts this PC, so removing it wasn't confirmed: remove it from "
                  "the sims on the site.")
        except CloudError as e:
            if not force:
                sys.exit(f"Not unpaired: {e}. Pass --force to forget the pairing here anyway (then remove this "
                         "PC from the sims on the site).")
            print(f"Shotcast Cloud didn't confirm removing this PC ({e}): remove it from the sims on the site.")
        if left:
            print(f"Left on this PC only, never sent ({prof.home}):\n{_describe(left)}")
        forget(st, prof.bound)


if __name__ == "__main__":
    main()
